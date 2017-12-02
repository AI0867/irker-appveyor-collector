irker-appveyor-collector
====================
An HTTP server that listens to AppVeyor webhook POSTs, collects and filters them, processes them into IRC messages and passes them on to irkerd.

irker-appveyor-collector.py
-----------------------
The server.

config.json
------------------
Configuration file

* ips: list of CIDR-notation IPs that are allowed to use this server
* port: port to listen on
* targets: dict of URLs (what the POST is submitted to) to target dicts:
    + project: must match the github organization name (or username, if personal repository)
    + channels: dict of email addresses or '\*' to lists of channels
* timeout: maximum number of seconds to wait for the rest of the appveyor reports
* max\_builds: maximum number of incomplete builds to store
