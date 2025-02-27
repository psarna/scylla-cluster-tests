# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2020 ScyllaDB

import os

import requests

from sdcm.remote import shell_script_cmd
from sdcm.tester import ClusterTester, teardown_on_exception
from sdcm.utils.decorators import log_run_info


JEPSEN_WEB_SERVER_START_DELAY = 15  # seconds
DB_SSH_KEY = "db_node_ssh_key"


class JepsenTest(ClusterTester):
    @property
    def jepsen_node(self):
        return self.loaders.nodes[0]

    @teardown_on_exception
    @log_run_info
    def setup_jepsen(self):
        remoter = self.jepsen_node.remoter
        remoter.sudo("apt-get install -y libjna-java gnuplot graphviz git")
        remoter.run(shell_script_cmd(f"""\
            curl -O https://raw.githubusercontent.com/technomancy/leiningen/stable/bin/lein
            chmod +x lein
            ./lein
            git clone {self.params.get('jepsen_scylla_repo')} jepsen-scylla
        """))
        for db_node in self.db_cluster.nodes:
            remoter.run(f"ssh-keyscan -t rsa {db_node.ip_address} >> ~/.ssh/known_hosts")
        remoter.send_files(os.path.expanduser(self.db_cluster.nodes[0].ssh_login_info["key_file"]), DB_SSH_KEY)

    def setUp(self):
        super().setUp()
        self.setup_jepsen()

    def test_jepsen(self):
        tests = self.params.get('jepsen_test_cmd')
        nodes = " ".join(f"--node {node.ip_address}" for node in self.db_cluster.nodes)
        creds = f"--username {self.db_cluster.nodes[0].ssh_login_info['user']} --ssh-private-key ~/{DB_SSH_KEY}"
        jepsen_cmd = f"cd ~/jepsen-scylla && ~/lein run {tests} {nodes} {creds}"

        self.log.info("Run Jepsen test: `%s'", jepsen_cmd)
        self.jepsen_node.remoter.run(jepsen_cmd)

    def save_jepsen_report(self):
        url = f"http://{self.jepsen_node.external_address}:8080/"

        self.log.info("Start web server to serve Jepsen results (will listen on %s)...", url)
        self.jepsen_node.remoter.run(shell_script_cmd(f"""\
            cd ~/jepsen-scylla
            setsid ~/lein run serve > save_jepsen_report.log 2>&1 < /dev/null &
            sleep {JEPSEN_WEB_SERVER_START_DELAY}
        """), verbose=True)

        with open(os.path.join(self.logdir, "jepsen_report.html"), "wt") as jepsen_report:
            jepsen_report.write(requests.get(url).text)
        self.log.info("Report has been saved to %s", jepsen_report.name)

        return jepsen_report.name

    def get_email_data(self):
        self.log.info("Prepare data for email")
        email_data = self._get_common_email_data()
        grafana_dataset = self.monitors.get_grafana_screenshot_and_snapshot(self.start_time) if self.monitors else {}
        email_data.update({
            "grafana_screenshots": grafana_dataset.get("screenshots", []),
            "grafana_snapshots": grafana_dataset.get("snapshots", []),
            "jepsen_report": self.save_jepsen_report(),
            "jepsen_scylla_repo": self.params.get("jepsen_scylla_repo"),
            "jepsen_test_cmd": self.params.get("jepsen_test_cmd"),
            "scylla_repo": self.params.get("scylla_repo"),
        })
        return email_data
