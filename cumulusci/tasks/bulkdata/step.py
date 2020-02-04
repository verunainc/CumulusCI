import csv
import io
import os
import pathlib
import requests
import tempfile
import time
import xml.etree.ElementTree as ET

from collections import namedtuple
from contextlib import contextmanager
from enum import Enum
from cumulusci.tasks.bulkdata.utils import BatchIterator
from cumulusci.core.exceptions import BulkDataException
from cumulusci.core.utils import process_bool_arg


class Operation(Enum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    HARD_DELETE = "hardDelete"
    QUERY = "query"


class Api(Enum):
    BULK = "bulk"
    REST = "rest"


class Status(Enum):
    SUCCESS = "Succeeded"
    FAILURE = "Failed"


Result = namedtuple("Result", ["id", "success", "error"])


@contextmanager
def download_file(uri, bulk_api):
    """Download the bulk API result file for a single batch"""
    (handle, path) = tempfile.mkstemp(text=False)
    resp = requests.get(uri, headers=bulk_api.headers(), stream=True)
    resp.raise_for_status()
    f = os.fdopen(handle, "wb")
    for chunk in resp.iter_content(chunk_size=None):
        f.write(chunk)

    f.close()
    with open(path, "r") as f:
        yield f

    pathlib.Path(path).unlink()


class BulkJobTaskMixin:
    def _job_state_from_batches(self, job_id):
        uri = f"{self.bulk.endpoint}/job/{job_id}/batch"
        response = requests.get(uri, headers=self.bulk.headers())
        response.raise_for_status()
        return self._parse_job_state(response.content)

    def _parse_job_state(self, xml):
        tree = ET.fromstring(xml)
        statuses = [el.text for el in tree.iterfind(".//{%s}state" % self.bulk.jobNS)]
        state_messages = [
            el.text for el in tree.iterfind(".//{%s}stateMessage" % self.bulk.jobNS)
        ]

        # FIXME: "Not Processed" to be expected for original batch with PK Chunking Query
        # PK Chunking is not currently supported.
        if "Not Processed" in statuses:
            return "Aborted", None
        elif "InProgress" in statuses or "Queued" in statuses:
            return "InProgress", None
        elif "Failed" in statuses:
            return "Failed", state_messages

        failures = tree.find(".//{%s}numberRecordsFailed" % self.bulk.jobNS)
        if failures is not None:
            num_failures = int(failures.text)
            if num_failures:
                return "CompletedWithFailures", [f"Failures detected: {num_failures}"]

        return "Completed", None

    def _wait_for_job(self, job_id):
        while True:
            job_status = self.bulk.job_status(job_id)
            self.logger.info(
                f"Waiting for job {job_id} ({job_status['numberBatchesCompleted']}/{job_status['numberBatchesTotal']})"
            )
            result, messages = self._job_state_from_batches(job_id)
            if result != "InProgress":
                break
            time.sleep(10)
        self.logger.info(f"Job {job_id} finished with result: {result}")
        if result == "Failed":
            for state_message in messages:
                self.logger.error(f"Batch failure message: {state_message}")

        return result


class Step:
    def __init__(self, sobject, operation, api_options, context):
        self.sobject = sobject
        self.operation = operation
        self.api_options = api_options
        self.context = context
        self.bulk = context.bulk
        self.sf = context.sf
        self.logger = context.logger
        self.status = None


class QueryStep(Step):
    def __init__(self, sobject, api_options, context, query):
        super().__init__(sobject, Operation.QUERY, api_options, context)
        self.soql = query

    def query(self):
        pass

    def get_results(self):
        pass


class BulkApiQueryStep(QueryStep, BulkJobTaskMixin):
    def query(self):
        self.job_id = self.bulk.create_query_job(self.sobject, contentType="CSV")
        self.batch_id = self.bulk.query(self.job_id, self.soql)

        result, errors = self._wait_for_job(self.job_id)
        if result == "Completed":
            self.status = Status.SUCCESS
        else:
            self.status = Status.FAILURE

        self.bulk.close_job(self.job_id)

    def get_results(self):
        # FIXME: For PK Chunking, need to get new batch Ids
        # and retrieve their results. Original batch will not be processed.

        result_ids = self.bulk.get_query_batch_result_ids(
            self.batch_id, job_id=self.job_id
        )
        for result_id in result_ids:
            uri = f"{self.bulk.endpoint}/job/{self.job_id}/batch/{self.batch_id}/result/{result_id}"

            with download_file(uri, self.bulk) as f:
                reader = csv.reader(f)
                self.headers = next(reader)
                if "Records not found for this query" in self.headers:
                    return

                yield from reader


class DmlStep(Step):
    def __init__(self, sobject, operation, api_options, context, fields):
        super().__init__(sobject, operation, api_options, context)
        self.fields = fields

    def start(self):
        pass

    def load_records(self, records):
        pass

    def end(self):
        pass

    def get_results(self):
        return []


class BulkApiDmlStep(DmlStep, BulkJobTaskMixin):
    def start(self):
        self.job_id = self.bulk.create_job(
            self.sobject,
            self.operation.value,
            contentType="CSV",
            concurrency=self.api_options.get("bulk_mode", "Parallel"),
        )

    def end(self):
        self.bulk.close_job(self.job_id)
        result = self._wait_for_job(self.job_id)
        if result == "Completed":
            self.status = Status.SUCCESS
        else:
            self.status = Status.FAILURE

    def load_records(self, records):
        self.batch_ids = []

        for count, batch_file in enumerate(self._batch(records)):
            self.context.logger.info(f"Uploading batch {count + 1}")
            self.batch_ids.append(self.bulk.post_batch(self.job_id, batch_file))

    def _batch(self, records):
        for batch in BatchIterator(records, self.api_options.get("batch_size", 10000)):
            yield self._csv_generator(batch)

    def _csv_generator(self, records):
        content = io.StringIO()
        writer = csv.writer(content)
        writer.writerow(self.fields)

        content.seek(0)
        yield content.read().encode("utf-8")
        for rec in records:
            content = io.StringIO()
            writer = csv.writer(content)
            writer.writerow(rec)
            content.seek(0)

            yield content.read().encode("utf-8")

    def get_results(self):
        for batch_id in self.batch_ids:
            try:
                results_url = (
                    f"{self.bulk.endpoint}/job/{self.job_id}/batch/{batch_id}/result"
                )
                # Download entire result file to a temporary file first
                # to avoid the server dropping connections
                with download_file(results_url, self.bulk) as f:
                    self.logger.info(f"Downloaded results for batch {batch_id}")

                    reader = csv.reader(f)
                    next(reader)  # skip header

                    for row in reader:
                        success = process_bool_arg(row[1])
                        yield Result(
                            row[0] if success else None,
                            success,
                            row[3] if not success else None,
                        )
            except Exception as e:
                raise BulkDataException(
                    f"Failed to download results for batch {batch_id} ({str(e)})"
                )
