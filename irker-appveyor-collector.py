#!/usr/bin/python3

import cidr
import collections
import http.client
import http.server
import json
import queue
import socket
import threading
import time
import traceback
import urllib.parse

CONFIG_FILE = "config.json"
CONFIG = json.load(open(CONFIG_FILE))

IRKER_PORT = 6659

COLORS = {'reset': '\x0f', 'yellow': '\x0307', 'green': '\x0303', 'bold': '\x02', 'red': '\x0305'}

event_queue = None

def format_build(build):
    failures = [job for job in build.jobs if job.status not in ["success", "cancelled"]]
    success = all([job.status == "success" for job in build.jobs])
    if success:
        status_string = "{green}All builds passed{reset}".format(**COLORS)
    elif not failures:
        status_string = "{green}Passed{reset}".format(**COLORS)
    else:
        status_string = "{red}{fail}/{num} Failed{reset}".format(fail=len(failures), num=len(build.jobs), **COLORS)

    messages = [
        "{bold}{repository}{reset}:{yellow}{branch}{reset} {green}{author}{reset} {bold}{commit}{reset} {message} {status}".format(
            repository  = build.repository,
            branch      = build.branch,
            author      = build.author,
            commit      = build.commit,
            message     = build.message[:40],
            status      = status_string,
            **COLORS
    )]
    for job in failures:
        messages.append("{bold}Details {yellow}{version}/{configuration}{reset}{bold}:{reset} {url}".format(
            version         = job.version,
            configuration   = job.configuration,
            url             = job.url,
            **COLORS
        ))
    return messages

def format_failure(build):
    jobs = [job for job in build.jobs if job.status not in ["success", "cancelled"]]
    if not jobs:
        return []
    configs = ",".join(["{version}/{configuration}".format(**job._asdict()) for job in jobs])
    messages = [
        "{bold}{repository}{reset}:{yellow}{branch}{reset} {green}{author}{reset} {bold}{commit}{reset} {message} {bold}{configs}{reset} {red}Failed".format(
            repository  = build.repository,
            branch      = build.branch,
            author      = build.author,
            commit      = build.commit,
            message     = build.message[:40],
            configs     = configs,
            **COLORS
    )]
    for url in set([job.url for job in jobs]):
        messages.append("{bold}Details: {reset}{url}".format(
            url=url,
            **COLORS
        ))
    return messages

def target_channels(channels, email):
    chans = []
    if "*" in channels:
        chans += channels["*"]
    if email in channels:
        chans += channels[email]
    return chans

def send_to_irker(message, channels):
    print("{chans}: {msg}".format(chans=",".join(channels), msg=message))
    envelope = { "to": channels, "privmsg": message }
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(json.dumps(envelope).encode("utf-8"), ("localhost", IRKER_PORT))

def check_peer(peer):
    address = cidr.CIDR(peer)
    masks = [cidr.CIDR(ip) for ip in CONFIG["ips"]]
    matches = [address in mask for mask in masks]
    return any(matches)

Build = collections.namedtuple("Build", ["repository", "branch", "commit", "author", "email", "message", "jobs", "num_versions", "first_report", "channels"])
Job = collections.namedtuple("Job", ["configuration", "version", "url", "status"])

def massage_json(blob, version, num_versions, channels):
    status_prefix = "build_"
    status = blob["eventName"][len(status_prefix):] if blob["eventName"].startswith(status_prefix) else blob["eventName"]
    repository = blob["eventData"]["repositoryName"]
    branch = blob["eventData"]["branch"]
    commit = blob["eventData"]["commitId"]
    author = blob["eventData"]["commitAuthor"]
    email = blob["eventData"]["commitAuthorEmail"]
    message = blob["eventData"]["commitMessage"]
    url = blob["eventData"]["buildUrl"]
    jobs = []
    for job in blob["eventData"]["jobs"]:
        job_status = job["status"].lower()
        configuration = job["name"]
        conf_prefix = "Configuration: "
        if configuration.startswith(conf_prefix):
            configuration = configuration[len(conf_prefix):]
        jobs.append(Job(configuration=configuration, version=version, url=url, status=job_status))
    build = Build(repository=repository, branch=branch, commit=commit, author=author, email=email, message=message, jobs=jobs, num_versions=int(num_versions), first_report=time.time(), channels=channels)
    return status, build

class ProcServer():
    def __init__(self, timeout, max_builds):
        self.running = True
        self.builds = []
        self.timeout = int(timeout)
        self.max_builds = int(max_builds)
    def shutdown(self):
        self.running = False
    def run(self):
        while self.running:
            if (self.builds and self.builds[0].first_report + self.timeout < time.time())\
              or len(self.builds) > self.max_builds:
                self.send_build(self.builds.pop(0))
            try:
                event = event_queue.get(True, 1)
            except queue.Empty:
                continue
            try:
                self.process_event(event)
            except RuntimeError as e:
                print("ProcServer: Exception caught: {}".format(e))
        # Shutting down. Send remaining builds
        for build in self.builds:
            self.send_build(build)
    def process_event(self, event):
        status, new_build = event
        old_builds = [build for build in self.builds if build.repository == new_build.repository and build.commit == new_build.commit]
        if len(old_builds) > 1:
            raise RuntimeError("Too many stored builds!")
        if old_builds:
            # Existing build
            build = old_builds[0]
            prev_good = all([job.status == "success" for job in build.jobs])
            build.jobs.extend(new_build.jobs)
            if build.num_versions != new_build.num_versions:
                self.send_build(build)
                self.builds.remove(build)
                raise RuntimeError("num_versions mismatch")
        else:
            # New build
            prev_good = True
            build = new_build
            self.builds.append(build)
        # Do we need to report now?
        if len(set([job.version for job in build.jobs])) == build.num_versions:
            # Full, send report
            self.send_build(build)
            self.builds.remove(build)
        elif prev_good and status != "success":
            # Report failure early, if not already reported,
            # but only the final report is not ready
            self.send_failure(new_build)
    def send_build(self, build):
        messages = format_build(build)
        self.send_messages(messages, build)
    def send_failure(self, build):
        messages = format_failure(build)
        self.send_messages(messages, build)
    def send_messages(self, messages, build):
        channels = target_channels(build.channels, build.email)
        try:
            for message in messages:
                send_to_irker(message, channels)
        except:
            traceback.print_exc()

class HttpHandler(http.server.BaseHTTPRequestHandler):
    def grab_json(self):
        rawblob = self.rfile.read(int(self.headers["content-length"]))
        utf8blob = rawblob.decode("utf-8")
        raw_content_type = self.headers["Content-Type"]
        content_type = raw_content_type.split(';')[0].strip()
        if content_type == "application/x-www-form-urlencoded":
            query = urllib.parse.parse_qsl(utf8blob)
            if len(query) == 1 and query[0][0] == "payload":
                return json.loads(query[0][1])
        elif content_type == "application/json":
            return json.loads(utf8blob)
        else:
            print("Invalid content-type: {}".format(self.headers["content-type"]))
        return None
    def do_POST(self):
        if check_peer(self.client_address[0]):
            target = CONFIG["targets"].get(self.path)
            if target:
                self.connection.settimeout(1) # Don't hang forever.
                jsonblob = self.grab_json()
                if jsonblob:
                    version = self.headers["Version"]
                    num_versions = self.headers["Num-Versions"]
                    status, build = massage_json(jsonblob, version, num_versions, target["channels"])
                    if target["repository"] == build.repository:
                        event_queue.put((status, build))
                    else:
                        print("Project mismatch {} {}".format(target["repository"], build.repository))
                else:
                    print("No payload")
            else:
                print("No target found for {}".format(self.path))
        else:
            print("Not AppVeyor: {}".format(self.client_address[0]))
        self.send_response(200)
        self.end_headers()

if __name__ == "__main__":
    event_queue = queue.Queue()

    httpserver = http.server.HTTPServer(("", CONFIG["port"]), HttpHandler)
    procserver = ProcServer(CONFIG["timeout"], CONFIG["max_builds"])
    httpthread = threading.Thread(target=httpserver.serve_forever)
    procthread = threading.Thread(target=procserver.run)

    httpthread.start()
    procthread.start()
    try:
        httpthread.join()
    except KeyboardInterrupt:
        print("Caught KeyboardInterrupt. Shutting down...")
        httpserver.shutdown()
        procserver.shutdown()
        httpthread.join()
        procthread.join()
