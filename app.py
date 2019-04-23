import asyncio
import collections
import json
import logging
import os
import re
import base64
import sys
import uuid
import watchtower

from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from tempfile import NamedTemporaryFile
from time import time

import tornado.ioloop
import tornado.web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError
from tornado.ioloop import IOLoop
from kafkahelpers import ReconnectingClient

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from kafka.errors import KafkaError
from utils import mnm, config
from logstash_formatter import LogstashFormatterV1
from prometheus_async.aio import time as prom_time
from boto3.session import Session


# Logging
LOGLEVEL = os.getenv("LOGLEVEL", "INFO")
if any("KUBERNETES" in k for k in os.environ):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogstashFormatterV1())
    logging.root.setLevel(LOGLEVEL)
    logging.root.addHandler(handler)
else:
    logging.basicConfig(
        level=LOGLEVEL,
        format="%(threadName)s %(levelname)s %(name)s - %(message)s"
    )

logger = logging.getLogger('upload-service')

NAMESPACE = config.get_namespace()

if (config.CW_AWS_ACCESS_KEY_ID and config.CW_AWS_SECRET_ACCESS_KEY):
    CW_SESSION = Session(aws_access_key_id=config.CW_AWS_ACCESS_KEY_ID,
                         aws_secret_access_key=config.CW_AWS_SECRET_ACCESS_KEY,
                         region_name=config.CW_AWS_REGION_NAME)
    cw_handler = watchtower.CloudWatchLogHandler(boto3_session=CW_SESSION,
                                                 log_group="platform",
                                                 stream_name=NAMESPACE)
    cw_handler.setFormatter(LogstashFormatterV1())
    logger.addHandler(cw_handler)

if not config.DEVMODE:
    VALID_TOPICS = config.get_valid_topics()

# Set Storage driver to use
storage = import_module("utils.storage.{}".format(config.STORAGE_DRIVER))

# Upload content type must match this regex. Third field matches end service
content_regex = r'^application/vnd\.redhat\.(?P<service>[a-z0-9-]+)\.(?P<category>[a-z0-9-]+).*'

kafka_consumer = AIOKafkaConsumer(config.VALIDATION_QUEUE, loop=IOLoop.current().asyncio_loop,
                                  bootstrap_servers=config.MQ, group_id=config.MQ_GROUP_ID)
kafka_producer = AIOKafkaProducer(loop=IOLoop.current().asyncio_loop, bootstrap_servers=config.MQ,
                                  request_timeout_ms=10000, connections_max_idle_ms=None)
CONSUMER = ReconnectingClient(kafka_consumer, "consumer")
PRODUCER = ReconnectingClient(kafka_producer, "producer")

# local queue for pushing items into kafka, this queue fills up if kafka goes down
produce_queue = collections.deque([], 999)

# Executor used to run non-async/blocking tasks
thread_pool_executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)


async def defer(*args):
    mnm.uploads_executor_qsize.set(thread_pool_executor._work_queue.qsize())
    res = await IOLoop.current().run_in_executor(None, *args)
    mnm.uploads_executor_qsize.set(thread_pool_executor._work_queue.qsize())
    return res

if config.DEVMODE:
    BUILD_DATE = 'devmode'
else:
    BUILD_DATE = config.get_commit_date(config.BUILD_ID)


def prepare_facts_for_inventory(facts):
    """
    Empty values need to be stripped from metadata prior to posting to inventory.
    Display_name must be greater than 1 and less than 200 characters.
    """
    defined_facts = {}
    for fact in facts:
        if facts[fact]:
            defined_facts.update({fact: facts[fact]})
    if 'display_name' in defined_facts and len(defined_facts['display_name']) not in range(2, 200):
        defined_facts.pop('display_name')
    return defined_facts


def get_service(content_type):
    """
    Returns the service that content_type maps to.
    """
    if content_type in config.SERVICE_MAP:
        return config.SERVICE_MAP[content_type]
    else:
        m = re.search(content_regex, content_type)
        if m:
            return m.groupdict()
    raise Exception("Could not resolve a service from the given content_type")


async def handle_validation(client):
    data = await client.getmany(timeout_ms=1000, max_records=30)
    for tp, msgs in data.items():
        if tp.topic == config.VALIDATION_QUEUE:
            await handle_file(msgs)


def make_preprocessor(queue=None):
    queue = produce_queue if queue is None else queue

    async def send_to_preprocessors(client):
        if not queue:
            await asyncio.sleep(0.1)
        else:
            item = queue.popleft()
            topic, msg, payload_id = item['topic'], item['msg'], item['msg'].get('payload_id')
            logger.info(
                "Popped data from produce queue (qsize now: %d) for topic [%s], payload_id [%s]: %s",
                len(queue), topic, payload_id, msg, extra={"request_id": payload_id, "account": msg["account"]}
            )
            try:
                await client.send_and_wait(topic, json.dumps(msg).encode("utf-8"))
                logger.info("send data for topic [%s] with payload_id [%s] succeeded", topic, payload_id, extra={"request_id": payload_id,
                                                                                                                 "account": msg["account"]})
            except KafkaError:
                queue.append(item)
                logger.error(
                    "send data for topic [%s] with payload_id [%s] failed, put back on queue (qsize now: %d)",
                    topic, payload_id, len(queue), extra={"request_id": payload_id,
                                                          "account": msg["account"]}
                )
                raise
    return send_to_preprocessors


@prom_time(mnm.uploads_handle_file_seconds)
async def handle_file(msgs):
    """Determine which bucket to put a payload in based on the message
       returned from the validating service.

    storage.copy operations are not async so we offload to the executor

    Arguments:
        msgs -- list of kafka messages consumed on validation topic
    """
    for msg in msgs:
        try:
            data = json.loads(msg.value)
        except ValueError:
            logger.error("handle_file(): unable to decode msg as json: {}".format(msg.value))
            continue

        if 'payload_id' not in data and 'hash' not in data:
            logger.error("payload_id or hash not in message. Payload not removed from permanent.")
            continue

        # get the payload_id. Getting the hash is temporary until consumers update
        payload_id = data['payload_id'] if 'payload_id' in data else data.get('hash')
        result = data.get('validation')

        logger.info('processing message: payload [%s] - %s', payload_id, result, extra={"request_id": payload_id,
                                                                                        "account": data["account"]})

        r = await defer(storage.ls, storage.PERM, payload_id)

        if r['ResponseMetadata']['HTTPStatusCode'] == 200:
            if result.lower() == 'success':
                mnm.uploads_validated.inc()

                url = await defer(storage.get_url, storage.PERM, payload_id)
                data = {
                    'topic': 'platform.upload.available',
                    'msg': {
                        'id': data.get('id'),
                        'url': url,
                        'service': data.get('service'),
                        'payload_id': payload_id,
                        'account': data.get('account'),
                        'principal': data.get('principal'),
                        'b64_identity': data.get('b64_identity'),
                        'satellite_managed': data.get('satellite_managed'),
                        'rh_account': data.get('account'),  # deprecated key, temp for backward compatibility
                        'rh_principal': data.get('principal'),  # deprecated key, temp for backward compatibility
                    }
                }
                produce_queue.append(data)
                logger.info(
                    "data for topic [%s], payload_id [%s] put on produce queue (qsize now: %d)",
                    data['topic'], payload_id, len(produce_queue), extra={"request_id": payload_id,
                                                                          "account": data["msg"]["account"]}
                )
                logger.debug("payload_id [%s] data: %s", payload_id, data)
            elif result.lower() == 'failure':
                mnm.uploads_invalidated.inc()
                logger.info('payload_id [%s] rejected', payload_id, extra={"request_id": payload_id,
                                                                           "account": data["account"]})
                url = await defer(storage.copy, storage.PERM, storage.REJECT, payload_id, data["account"])
            elif result.lower() == 'handoff':
                mnm.uploads_handed_off.inc()
                logger.info('payload_id [%s] handed off', payload_id, extra={"request_id": payload_id,
                                                                             "account": data["account"]})
            else:
                logger.info('Unrecognized result: %s', result.lower(), extra={"request_id": payload_id,
                                                                              "account": data["account"]})
        else:
            logger.info('payload_id [%s] no longer in permanent bucket', payload_id, extra={"request_id": payload_id,
                                                                                            "account": data["account"]})


async def post_to_inventory(identity, payload_id, values):
    headers = {'x-rh-identity': identity,
               'Content-Type': 'application/json',
               'x-rh-insights-request-id': payload_id,
               }
    post = prepare_facts_for_inventory(values['metadata'])
    post['account'] = values['account']
    post = json.dumps([post])
    try:
        httpclient = AsyncHTTPClient()
        response = await httpclient.fetch(config.INVENTORY_URL, body=post, headers=headers, method="POST")
        body = json.loads(response.body)
        if response.code != 207:
            mnm.uploads_inventory_post_failure.inc()
            error = body.get('detail')
            logger.error('Failed to post to inventory: %s', error, extra={"request_id": payload_id,
                                                                          "account": values["account"]})
            logger.debug('Host data that failed to post: %s' % post, extra={"request_id": payload_id,
                                                                            "account": values["account"]})
            return None
        elif body['data'][0]['status'] != 200 and body['data'][0]['status'] != 201:
            mnm.uploads_inventory_post_failure.inc()
            error = body['data'][0].get('detail')
            logger.error('Failed to post to inventory: ' + error, extra={"request_id": payload_id,
                                                                         "account": values["account"]})
            logger.debug('Host data that failed to post: %s' % post, extra={"request_id": payload_id,
                                                                            "account": values["account"]})
            return None
        else:
            mnm.uploads_inventory_post_success.inc()
            inv_id = body['data'][0]['host']['id']
            logger.info('Payload [%s] posted to inventory. ID [%s]', payload_id, inv_id, extra={"request_id": payload_id,
                                                                                                "id": inv_id,
                                                                                                "account": values["account"]})
            return inv_id
    except HTTPClientError:
        logger.error("Unable to contact inventory", extra={"request_id": payload_id,
                                                           "account": values["account"]})


class NoAccessLog(tornado.web.RequestHandler):
    """
    A class to override tornado's logging mechanism.
    Reduce noise in the logs via GET requests we don't care about.
    """

    def _log(self):
        if LOGLEVEL == "DEBUG":
            super()._log()
        else:
            pass


class RootHandler(NoAccessLog):
    """Handles requests to document root
    """

    def get(self):
        """Handle GET requests to the root url
        ---
        description: Used for OpenShift Liveliness probes
        responses:
            200:
                description: OK
                content:
                    text/plain:
                        schema:
                            type: string
                            example: boop
        """
        self.write("boop")

    def options(self):
        """Return a header containing the available methods
        ---
        description: Add a header containing allowed methods
        responses:
            200:
                description: OK
                headers:
                    Allow:
                        description: Allowed methods
                        schema:
                            type: string
        """
        self.add_header('Allow', 'GET, HEAD, OPTIONS')


class UploadHandler(tornado.web.RequestHandler):
    """Handles requests to the upload endpoint
    """
    def upload_validation(self):
        """Validate the upload using general criteria

        Returns:
            tuple -- status code and a user friendly message
        """
        content_length = int(self.request.headers["Content-Length"])
        if content_length >= config.MAX_LENGTH:
            mnm.uploads_too_large.inc()
            logger.error("Payload too large. Request ID [%s] - Length %s", self.payload_id, str(config.MAX_LENGTH), extra={"request_id": self.payload_id})
            return self.error(413, f"Payload too large: {content_length}. Should not exceed {config.MAX_LENGTH} bytes")
        try:
            serv_dict = get_service(self.payload_data['content_type'])
        except Exception:
            mnm.uploads_unsupported_filetype.inc()
            logger.error("Unsupported Media Type: [%s] - Request-ID [%s]", self.payload_data['content_type'], self.payload_id, extra={"request_id": self.payload_id})
            return self.error(415, 'Unsupported Media Type')
        if not config.DEVMODE and serv_dict["service"] not in VALID_TOPICS:
            logger.error("Unsupported MIME type: [%s] - Request-ID [%s]", self.payload_data['content_type'], self.payload_id, extra={"request_id": self.payload_id})
            return self.error(415, 'Unsupported MIME type')

    def get(self):
        """Handles GET requests to the upload endpoint
        ---
        description: Get accepted content types
        responses:
            200:
                description: OK
                content:
                    text/plain:
                        schema:
                            type: string
                            example: 'Accepted Content-Types: gzipped tarfile, zip file'
        """
        self.write("Accepted Content-Types: gzipped tarfile, zip file")

    async def upload(self, filename, tracking_id, payload_id, identity):
        """Write the payload to the configured storage

        Storage write and os file operations are not async so we offload to executor.

        Arguments:
            filename {str} -- The filename to upload. Should be the tmpfile
                              created by `write_data`
            tracking_id {str} -- The tracking ID sent by the client
            payload_id {str} -- the unique ID for this upload generated by 3Scale at time of POST

        Returns:
            str -- URL of uploaded file if successful
            None if upload failed
        """
        account = self.identity.get("account_number")
        user_agent = self.request.headers.get("User-Agent")

        upload_start = time()
        logger.info("tracking id [%s] payload_id [%s] attempting upload", tracking_id, payload_id, extra={"request_id": payload_id,
                                                                                                          "account": account})
        try:
            url = await defer(storage.write, filename, storage.PERM, payload_id,
                              account, user_agent)
            elapsed = time() - upload_start

            logger.info(
                "tracking id [%s] payload_id [%s] uploaded! elapsed [%fsec] url [%s]",
                tracking_id, payload_id, elapsed, url, extra={"request_id": payload_id,
                                                              "account": account}
            )

            return url
        except Exception:
            elapsed = time() - upload_start
            logger.exception(
                "Exception hit uploading: tracking id [%s] payload_id [%s] elapsed [%fsec]",
                tracking_id, payload_id, elapsed, extra={"request_id": payload_id, "account": account}
            )
        finally:
            await defer(os.remove, filename)

    async def process_upload(self):
        """Process the uploaded file we have received.

        Arguments:
            filename {str} -- The filename to upload. Should be the tmpfile
                              created by `write_data`
            size {int} -- content-length of the uploaded filename
            tracking_id {str} -- The tracking ID sent by the client
            payload_id {str} -- the unique ID for this upload generated by 3Scale at time of POST
            identity {str} -- identity pulled from request headers (if present)
            service {str} -- The service this upload is intended for

        Write to storage, send message to MQ
        """
        values = {}
        # use dummy values for now if no account given
        if self.identity:
            values['account'] = self.identity['account_number']
            values['rh_account'] = self.identity['account_number']
            values['principal'] = self.identity['internal'].get('org_id') if self.identity.get('internal') else None
        else:
            values['account'] = config.DUMMY_VALUES['account']
            values['principal'] = config.DUMMY_VALUES['principal']
        values['payload_id'] = self.payload_id
        values['hash'] = self.payload_id  # provided for backward compatibility
        values['size'] = self.size
        values['service'] = self.service
        values['category'] = self.category
        values['b64_identity'] = self.b64_identity
        if self.metadata:
            values['metadata'] = json.loads(self.metadata)
            values['id'] = await post_to_inventory(self.b64_identity, self.payload_id, values)
            del values['metadata']

        url = await self.upload(self.filename, self.tracking_id, self.payload_id, self.identity)

        if url:
            values['url'] = url
            topic = 'platform.upload.' + self.service
            produce_queue.append({'topic': topic, 'msg': values})
            logger.info(
                "Data for payload_id [%s] to topic [%s] put on produce queue (qsize now: %d)",
                self.payload_id, topic, len(produce_queue), extra={"request_id": self.payload_id,
                                                                   "account": values["account"]}
            )

    @mnm.uploads_write_tarfile.time()
    def write_data(self, body):
        """Writes the uploaded data to a tmp file in prepartion for writing to
           storage

        OS file operations are not async so this should run in executor.

        Arguments:
            body -- upload body content

        Returns:
            str -- tmp filename so it can be uploaded
        """
        with NamedTemporaryFile(delete=False) as tmp:
            tmp.write(body)
            tmp.flush()
            filename = tmp.name
        return filename

    def error(self, code, message, **kwargs):
        logger.error(message, extra=kwargs)
        self.set_status(code, message)
        self.set_header("Content-Type", "text/plain")
        self.write(message)
        return (code, message)

    @prom_time(mnm.uploads_post_time)
    async def post(self):
        """Handle POST requests to the upload endpoint

        Validate upload, get service name, create UUID, save to local storage,
        then offload for async processing
        ---
        description: Process Insights archive
        responses:
            202:
                description: Upload payload accepted
            413:
                description: Payload too large
            415:
                description: Upload field not found
        """
        mnm.uploads_total.inc()
        self.identity = None
        request_id = self.request.headers.get('x-rh-insights-request-id')
        self.payload_id = request_id if request_id else uuid.uuid4().hex

        if not self.request.files.get('upload') and not self.request.files.get('file'):
            return self.error(
                415,
                "Upload field not found",
                files=list(self.request.files),
                request_id=self.payload_id,
            )

        # TODO: pull this out once no one is using the upload field anymore
        self.payload_data = self.request.files.get('upload')[0] if self.request.files.get('upload') else self.request.files.get('file')[0]

        if self.payload_id is None:
            return self.error(400, "No payload_id assigned.  Upload failed.")

        if self.upload_validation():
            mnm.uploads_invalid.inc()
        else:
            mnm.uploads_valid.inc()
            self.tracking_id = str(self.request.headers.get('Tracking-ID', "null"))
            self.metadata = self.__get_metadata_from_request()
            service_dict = get_service(self.payload_data['content_type'])
            self.service = service_dict["service"]
            self.category = service_dict["category"]
            if self.request.headers.get('x-rh-identity'):
                header = json.loads(base64.b64decode(self.request.headers['x-rh-identity']))
                self.identity = header['identity']
                self.b64_identity = self.request.headers['x-rh-identity']
            self.size = int(self.request.headers['Content-Length'])
            body = self.payload_data['body']

            self.filename = await defer(self.write_data, body)

            self.set_status(202, "Accepted")

            # Offload the handling of the upload and producing to kafka
            asyncio.ensure_future(
                self.process_upload()
            )

    def options(self):
        """Handle OPTIONS request to upload endpoint
        ---
        description: Add a header containing allowed methods
        responses:
            200:
                description: OK
                headers:
                    Allow:
                        description: Allowed methods
                        schema:
                            type: string
        """
        self.add_header('Allow', 'GET, POST, HEAD, OPTIONS')

    def __get_metadata_from_request(self):
        if self.request.files.get('metadata'):
            return self.request.files['metadata'][0]['body'].decode('utf-8')
        elif self.request.body_arguments.get('metadata'):
            return self.request.body_arguments['metadata'][0].decode('utf-8')


class VersionHandler(tornado.web.RequestHandler):
    """Handler for the `version` endpoint
    """

    def get(self):
        """Handle GET request to the `version` endpoint
        ---
        description: Get version identifying information
        responses:
            200:
                description: OK
                content:
                    application/json:
                        schema:
                            type: object
                            properties:
                                commit:
                                    type: string
                                    example: ab3a3a90b48bb1101a287b754d33ac3b2316fdf2
                                date:
                                    type: string
                                    example: '2019-03-19T14:17:27Z'
        """
        response = {'commit': config.BUILD_ID,
                    'date': BUILD_DATE}
        self.write(response)


class MetricsHandler(NoAccessLog):
    """Handle requests to the metrics
    """

    def get(self):
        """Get metrics for upload service
        ---
        description: Get metrics for upload service
        responses:
            200:
                description: OK
                content:
                    text/plain:
                        schema:
                            type: string
        """
        self.write(mnm.generate_latest())


class SpecHandler(tornado.web.RequestHandler):
    """Handle requests for service's API Spec
    """

    def get(self):
        """Get the openapi/swagger spec for the upload service
        ---
        description: Get openapi spec for upload service
        responses:
            200:
                description: OK
        """
        response = config.spec.to_dict()
        self.write(response)


endpoints = [
    (config.API_PREFIX, RootHandler),
    (config.API_PREFIX + "/v1/version", VersionHandler),
    (config.API_PREFIX + "/v1/upload", UploadHandler),
    (config.API_PREFIX + "/v1/openapi.json", SpecHandler),
    (r"/r/insights/platform/upload", RootHandler),
    (r"/r/insights/platform/upload/api/v1/version", VersionHandler),
    (r"/r/insights/platform/upload/api/v1/upload", UploadHandler),
    (r"/r/insights/platform/upload/api/v1/openapi.json", SpecHandler),
    (r"/metrics", MetricsHandler)
]

for urlSpec in endpoints:
    config.spec.path(urlspec=urlSpec)

app = tornado.web.Application(endpoints, max_body_size=config.MAX_LENGTH)


def main():
    app.listen(config.LISTEN_PORT)
    logger.info(f"Web server listening on port {config.LISTEN_PORT}")
    loop = IOLoop.current()
    loop.set_default_executor(thread_pool_executor)
    loop.spawn_callback(CONSUMER.get_callback(handle_validation))
    loop.spawn_callback(PRODUCER.get_callback(make_preprocessor(produce_queue)))
    try:
        loop.start()
    except KeyboardInterrupt:
        loop.stop()


if __name__ == "__main__":
    main()
