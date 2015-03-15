# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import datetime
import logging
import multiprocessing
import os.path
import threading
import time

from flask import json
from lib.api import node_status, submit_task, fetch_tasks
from lib.api import store_report, delete_task
from lib.db import db, Node, Task

log = logging.getLogger(__name__)

def nullcallback(arg):
    return arg

class SchedulerThread(threading.Thread):
    def __init__(self, app):
        threading.Thread.__init__(self)

        self.app = app
        self.available = {}

    def _mark_available(self, name):
        """Mark a node as available for scheduling."""
        self.available[name] = self.app.config["INTERVAL"]

        log.debug("Logging node %s as available..", name)

    def _node_status(self, (name, status)):
        self.app.config["STATUSES"][name] = status

        with self.app.app_context():
            node = Node.query.filter_by(name=name).first()

        # If available, we'll want to dump the uptime.
        if self.app.config["UPTIME_LOGFILE"]:
            try:
                with open(self.app.config["UPTIME_LOGFILE"], "ab") as f:
                    timestamp = datetime.datetime.now().strftime("%s")
                    d = dict(timestamp=timestamp, name=name, status=status)
                    f.write(json.dumps(d) + "\n")
            except Exception as e:
                log.warning("Error dumping uptime for node %r: %s", name, e)

        log.debug("Node %s status %s", name, status)

        if not status:
            self._mark_available(name)
            return

        if status["pending"] < self.app.config["BATCH_SIZE"]:
            with self.app.app_context():
                self.submit_tasks(name, self.app.config["BATCH_SIZE"])

        args = node.name, node.url, "reported"
        self.m.apply_async(fetch_tasks, args=args,
                           callback=self._fetch_reports_and_mark_available)

    def _task_identifier(self, (task_id, api_task_id)):
        with self.app.app_context():
            t = Task.query.get(task_id)
            t.task_id = api_task_id
            db.session.commit()

            log.debug("Node %s task %d -> %d", t.node_id, t.task_id, t.id)

    def _fetch_reports_and_mark_available(self, (name, tasks)):
        with self.app.app_context():
            node = Node.query.filter_by(name=name).first()

            for task in tasks:
                print "task", task
                q = Task.query.filter_by(node_id=node.id, task_id=task["id"])
                t = q.first()

                if t is None:
                    log.debug("Node %s task #%d has not been submitted "
                              "by us!", name, task["id"])
                    args = node.name, node.url, task["id"]
                    self.m.apply_async(delete_task, args=args)
                    continue

                dirpath = os.path.join(self.app.config["REPORTS_DIRECTORY"],
                                       "%d" % t.id)

                if not os.path.isdir(dirpath):
                    os.makedirs(dirpath)

                # Fetch each requested report format, request this report.
                for report_format in self.app.config["REPORT_FORMATS"]:
                    args = [
                        node.name, node.url, t.task_id,
                        report_format, dirpath,
                    ]
                    self.m.apply_async(store_report, args=args,
                                       callback=self._store_report)

                t.finished = True

            db.session.commit()

            # Mark as available after all stuff has happened.
            self.m.apply_async(nullcallback, args=(name,),
                               callback=self._mark_available)

    def _store_report(self, (name, task_id, report_format)):
        with self.app.app_context():
            node = Node.query.filter_by(name=name).first()

            # Delete the task and all its associated files.
            args = node.name, node.url, task_id
            self.m.apply_async(delete_task, args=args)

    def submit_tasks(self, name, count):
        """Submit count tasks to a Cuckoo node."""
        # TODO Handle priority other than 1.
        # TODO Select only the tasks with appropriate tags selection.

        # Select tasks that have already been selected for this node, but have
        # not been submitted due to an unexpected exit of the program or so.
        # TODO Revive this code. Since task_id is assigned asynchronously,
        # make sure this doesn't introduce problems.
        # tasks = Task.query.filter_by(node_id=node.id, task_id=None)

        node = Node.query.filter_by(name=name).first()

        # Select regular tasks.
        tasks = Task.query.filter_by(node_id=None, finished=False)
        tasks = tasks.filter_by(priority=1)
        tasks = tasks.order_by(Task.id).limit(count)

        # Update all tasks to use our node id.
        for task in tasks.all():
            task.node_id = node.id
            args = node.name, node.url, task.to_dict()
            self.m.apply_async(submit_task, args=args,
                               callback=self._task_identifier)

        # Commit these changes.
        db.session.commit()

    def handle_node(self, node):
        if node.name not in self.available:
            self.available[node.name] = 1
            log.info("Detected Cuckoo node '%s': %s", node.name, node.url)

        # This node is currently being processed.
        if not self.available[node.name]:
            log.debug("Node is currently processing: %s", node.name)
            return

        # Decrease waiting time by one second.
        self.available[node.name] -= 1

        # If available returns 0 for this node then it's time to
        # schedule this node again.
        if not self.available[node.name]:
            self.m.apply_async(node_status, args=(node.name, node.url),
                               callback=self._node_status)
        else:
            log.debug("Node waiting (%d): %s..",
                      self.available[node.name], node.name)

    def run(self):
        self.m = multiprocessing.Pool(
            processes=self.app.config["WORKER_PROCESSES"])

        while self.app.config["RUNNING"]:
            # We resolve the nodes every iteration, that way new nodes may
            # be added on-the-fly.
            with self.app.app_context():
                for node in Node.query.filter_by(enabled=True).all():
                    self.handle_node(node)

            time.sleep(1)

        self.m.close()
