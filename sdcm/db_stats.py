import re
import datetime
import time
import os
import subprocess
import platform
import logging
import json
from textwrap import dedent
from math import sqrt
from collections import defaultdict

import yaml
import requests

from sdcm.es import ES
from sdcm.utils.common import get_job_name, normalize_ipv6_url
from sdcm.utils.decorators import retrying

LOGGER = logging.getLogger(__name__)


class CassandraStressCmdParseError(Exception):
    def __init__(self, cmd, ex):
        super(CassandraStressCmdParseError, self).__init__()
        self.command = cmd
        self.exception = repr(ex)

    def __str__(self):
        return dedent("""
            Stress command: '{0.command}'
            Error: {0.exception}""".format(self))

    def __repr__(self):
        return self.__str__()


def stddev(lst):
    mean = float(sum(lst)) / len(lst)
    return sqrt(sum((x - mean)**2 for x in lst) / len(lst))


def get_stress_cmd_params(cmd):
    """
    Parsing cassandra stress command
    :param cmd: stress cmd
    :return: dict with params
    """

    # pylint: disable=too-many-branches
    cmd_params = {
        "raw_cmd": cmd
    }
    try:
        cmd = cmd.strip().split('cassandra-stress')[1].strip()
        if cmd.split(' ')[0] in ['read', 'write', 'mixed', 'counter_write', 'user']:
            cmd_params['command'] = cmd.split(' ')[0]
            if 'no-warmup' in cmd:
                cmd_params['no-warmup'] = True

            match = re.search(r'(cl\s?=\s?\w+)', cmd)
            if match:
                cmd_params['cl'] = match.group(0).split('=')[1].strip()

            match = re.search(r'(duration\s?=\s?\w+)', cmd)
            if match:
                cmd_params['duration'] = match.group(0).split('=')[1].strip()

            match = re.search(r'( n\s?=\s?\w+)', cmd)
            if match:
                cmd_params['n'] = match.group(0).split('=')[1].strip()
            match = re.search(r'profile=(\S+)\s+', cmd)
            if match:
                cmd_params['profile'] = match.group(1).strip()
                match = re.search(r'ops(\S+)\s+', cmd)
                if match:
                    cmd_params['ops'] = match.group(1).split('=')[0].strip('(')

            for temp in cmd.split(' -')[1:]:
                try:
                    key, value = temp.split(" ", 1)
                except ValueError as ex:
                    LOGGER.warning("%s:%s", temp, ex)
                else:
                    cmd_params[key] = value.strip().replace("'", "")
            if 'rate' in cmd_params:
                # split rate section on separate items
                if 'threads' in cmd_params['rate']:
                    cmd_params['rate threads'] = \
                        re.search(r'(threads\s?=\s?(\w+))', cmd_params['rate']).group(2)
                if 'throttle' in cmd_params['rate']:
                    cmd_params['throttle threads'] =\
                        re.search(r'(throttle\s?=\s?(\w+))', cmd_params['rate']).group(2)
                if 'fixed' in cmd_params['rate']:
                    cmd_params['fixed threads'] =\
                        re.search(r'(fixed\s?=\s?(\w+))', cmd_params['rate']).group(2)
                del cmd_params['rate']

        return cmd_params
    except Exception as ex:
        raise CassandraStressCmdParseError(cmd=cmd, ex=ex)


def get_stress_bench_cmd_params(cmd):
    """
    Parsing bench stress command
    :param cmd: stress cmd
    :return: dict with params
    """
    cmd = cmd.strip().split('scylla-bench')[1].strip()
    cmd_params = {}
    for key in ['partition-count', 'clustering-row-count', 'clustering-row-size', 'mode',
                'workload', 'concurrency', 'max-rate', 'connection-count', 'replication-factor',
                'timeout', 'client-compression', 'duration']:
        match = re.search(r'(-' + key + r'\s+([^-| ]+))', cmd)
        if match:
            cmd_params[key] = match.group(2).strip()
    return cmd_params


def get_ycsb_cmd_params(cmd):
    """
    Parsing ycsb command
    :param cmd: stress cmd
    :return: dict with params
    """

    cmd_params = {
        "raw_cmd": cmd
    }

    threads_regex = re.compile(r'-threads\s*(.*?)[\s$]')
    key_value_regex = re.compile(r"-p\s.*?(?P<key>.*?)=(?P<value>.*?)(\s|$)")
    for match in key_value_regex.finditer(cmd):
        match_dict = match.groupdict()
        cmd_params[match_dict['key']] = match_dict['value']
    match = threads_regex.search(cmd)
    if match:
        cmd_params['threads'] = match.group(1)
    return cmd_params


def get_raw_cmd_params(cmd):
    cmd = cmd.strip()
    cmd_params = {
        'raw_cmd': cmd
    }
    return cmd_params


def get_gemini_cmd_params(cmd):
    return get_raw_cmd_params(cmd)


def get_cdcreader_cmd_params(cmd):
    return get_raw_cmd_params(cmd)


class PrometheusDBStats():
    def __init__(self, host, port=9090, alternator=None):
        self.host = host
        self.port = port
        self.range_query_url = "http://{}:{}/api/v1/query_range?query=".format(normalize_ipv6_url(host), port)
        self.config = self.get_configuration()
        self.alternator = alternator

    @property
    def scylla_scrape_interval(self):
        return int(self.config["scrape_configs"]["scylla"]["scrape_interval"][:-1])

    @staticmethod
    @retrying(n=5, sleep_time=7, allowed_exceptions=(requests.ConnectionError, requests.HTTPError))
    def request(url, post=False):
        if post:
            response = requests.post(url)
        else:
            response = requests.get(url)
        response.raise_for_status()

        result = json.loads(response.content)
        LOGGER.debug("Response from Prometheus server: %s", str(result)[:200])
        if result["status"] == "success":
            return result
        else:
            LOGGER.error("Prometheus returned error: %s", result)
        return None

    def get_configuration(self):
        result = self.request(url="http://{}:{}/api/v1/status/config".format(normalize_ipv6_url(self.host), self.port))
        configs = yaml.safe_load(result["data"]["yaml"])
        LOGGER.debug("Parsed Prometheus configs: %s", configs)
        new_scrape_configs = {}
        for conf in configs["scrape_configs"]:
            new_scrape_configs[conf["job_name"]] = conf
        configs["scrape_configs"] = new_scrape_configs
        return configs

    def query(self, query, start, end, scrap_metrics_step=None):
        """
        :param start: time=<rfc3339 | unix_timestamp>: Start timestamp.
        :param end: time=<rfc3339 | unix_timestamp>: End timestamp.
        :param scrap_metrics_step is the granularity of data requested from Prometheus DB
        :param query:
        :return: {
                  metric: { },
                  values: [[linux_timestamp1, value1], [linux_timestamp2, value2]...[linux_timestampN, valueN]]
                 }
        """
        url = "http://{}:{}/api/v1/query_range?query=".format(normalize_ipv6_url(self.host), self.port)
        if not scrap_metrics_step:
            scrap_metrics_step = self.scylla_scrape_interval
        _query = "{url}{query}&start={start}&end={end}&step={scrap_metrics_step}".format(
            url=url, query=query, start=start, end=end, scrap_metrics_step=scrap_metrics_step)
        LOGGER.debug("Query to PrometheusDB: %s", _query)
        result = self.request(url=_query)
        if result:
            return result["data"]["result"]
        else:
            LOGGER.error("Prometheus query unsuccessful!")
            return []

    @staticmethod
    def _check_start_end_time(start_time, end_time):
        if end_time - start_time < 120:
            LOGGER.warning("Time difference too low to make a query [start_time: %s, end_time: %s", start_time,
                           end_time)
            return False
        return True

    def _get_query_values(self, query, start_time, end_time, scrap_metrics_step=None):
        results = self.query(query=query, start=start_time, end=end_time, scrap_metrics_step=scrap_metrics_step)
        if results:
            return results[0]["values"]
        else:
            return []

    def get_throughput(self, start_time, end_time, scrap_metrics_step=None):
        """
        Get Scylla throughput (ops/second) from PrometheusDB

        :return: list of tuples (unix time, op/s)
        """
        if not self._check_start_end_time(start_time, end_time):
            return []
        # the query is taken from the Grafana Dashborad definition
        if self.alternator:
            query = "sum(irate(scylla_alternator_operation{}[30s]))"
        else:
            query = "sum(irate(scylla_transport_requests_served{}[30s]))%20%2B%20sum(irate(scylla_thrift_served{}[30s]))"
        return self._get_query_values(query, start_time, end_time, scrap_metrics_step=scrap_metrics_step)

    def get_scylla_reactor_utilization(self, start_time, end_time, scrap_metrics_step=None):
        """
        Get Scylla CPU (avg) from PrometheusDB

        :return: list of tuples (unix time, op/s)
        """
        if not self._check_start_end_time(start_time, end_time):
            return []
        query = "avg(scylla_reactor_utilization{})"
        res = self._get_query_values(query, start_time, end_time, scrap_metrics_step=scrap_metrics_step)
        if res:
            res = [float(value[1]) for value in res]
            return sum(res) / len(res)
        else:
            return res

    def get_scylla_scheduler_runtime_ms(self, start_time, end_time, node_ip):
        """
        Get Scylla CPU scheduler runtime from PrometheusDB

        :return: list of tuples (unix time, op/s)
        """
        if not self._check_start_end_time(start_time, end_time):
            return {}
        # the query is taken from the Grafana Dashborad definition
        query = 'avg(irate(scylla_scheduler_runtime_ms{group=~"service_level_.*", instance="%s"}  [30s] )) ' \
            'by (group, instance)' % node_ip
        results = self.query(query=query, start=start_time, end=end_time)
        res = defaultdict(dict)
        for item in results:
            res[item['metric']['instance']].update({item['metric']['group']:
                                                    [float(runtime[1]) for runtime in item['values']]})
        return res

    def get_scylla_scheduler_shares_per_sla(self, start_time, end_time, node_ip):  # pylint: disable=invalid-name
        """
        Get scylla_scheduler_shares from PrometheusDB

        :return: list of tuples (unix time, op/s)
        """
        if not self._check_start_end_time(start_time, end_time):
            return {}
        # the query is taken from the Grafana Dashborad definition
        query = 'avg(scylla_scheduler_shares{group=~"service_level_.*", instance="%s"} ) by (group, instance)' % node_ip
        results = self.query(query=query, start=start_time, end=end_time)
        res = {}
        for item in results:
            res[item['metric']['group']] = {int(i[1]) for i in item['values']}
        return res

    def get_latency(self, start_time, end_time, latency_type, scrap_metrics_step=None):
        """latency values are returned in microseconds"""
        assert latency_type in ["read", "write"]
        if not self._check_start_end_time(start_time, end_time):
            return []
        query = "histogram_quantile(0.99, sum(rate(scylla_storage_proxy_" \
            "coordinator_%s_latency_bucket{}[30s])) by (le))" % latency_type
        return self._get_query_values(query, start_time, end_time, scrap_metrics_step=scrap_metrics_step)

    def get_latency_read_99(self, start_time, end_time, scrap_metrics_step=None):
        return self.get_latency(start_time, end_time, latency_type="read",
                                scrap_metrics_step=scrap_metrics_step)

    def get_latency_write_99(self, start_time, end_time, scrap_metrics_step=None):
        return self.get_latency(start_time, end_time, latency_type="write",
                                scrap_metrics_step=scrap_metrics_step)

    def create_snapshot(self):
        url = "http://{}:{}/api/v1/admin/tsdb/snapshot".format(normalize_ipv6_url(self.host), self.port)
        result = self.request(url, True)
        LOGGER.debug('Request result: {}'.format(result))
        return result


class Stats():
    """
    This class is responsible for creating and updating database entry(document in Elasticsearch DB)
    There are two usage options:
    1. without arguments - as a based class of TestStatsMixin - for saving test statistics
    2. with arguments - as a separate object to update an existing document
    """

    def __init__(self, *args, **kwargs):
        self._test_index = kwargs.get('test_index', None)
        self._test_id = kwargs.get('test_id', None)
        self._es_doc_type = "test_stats"
        self.elasticsearch = self._create_es_connection()
        self._stats = {}
        if not self._test_id:
            super(Stats, self).__init__(*args, **kwargs)

    @staticmethod
    def _create_es_connection():
        return ES()

    def get_doc_id(self):
        return self._test_id

    def get_stats(self):
        return self._stats

    def create(self):
        self.elasticsearch.create_doc(index=self._test_index, doc_type=self._es_doc_type,
                                      doc_id=self._test_id, body=self._stats)

    def update(self, data):
        """
        Update document
        :param data: data dictionary
        """
        try:
            self.elasticsearch.update_doc(index=self._test_index, doc_type=self._es_doc_type,
                                          doc_id=self._test_id, body=data)
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.error('Failed to update test stats: test_id: %s, error: %s', self._test_id, ex)

    def exists(self):
        return self.elasticsearch.exists(index=self._test_index, doc_type=self._es_doc_type, id=self._test_id)


class TestStatsMixin(Stats):
    """
    This mixin is responsible for saving test details and statistics in database.
    """
    KEYS = {
        'test_details': dict(),
        'setup_details': dict(),
        'versions': dict(),
        'results': dict(),
        'nemesis': dict(),
        'errors': list(),
        'coredumps': dict(),
    }
    PROMETHEUS_STATS = ('throughput', 'latency_read_99', 'latency_write_99')
    PROMETHEUS_STATS_UNITS = {'throughput': "op/s", 'latency_read_99': "us", 'latency_write_99': "us"}
    STRESS_STATS = ('op rate', 'latency mean', 'latency 99th percentile')
    STRESS_STATS_TOTAL = ('op rate', 'Total errors')

    @staticmethod
    def _create_test_id(doc_id_with_timestamp=False):
        """Return doc_id equal unified test-id

        Generate doc_id for ES document as unified global test-id
        if doc_id_with_timestamp is true, create ES
        document with global test_id + timestamp

        :param doc_id_with_timestamp: add timestamp to test_id , defaults to False
        :type doc_id_with_timestamp: bool, optional
        :returns: doc_id for document in ES
        :rtype: {str}
        """
        # avoid cyclic-decencies between cluster and db_stats
        from sdcm.cluster import Setup
        doc_id = Setup.test_id()
        if doc_id_with_timestamp:
            doc_id += "_{}".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        return doc_id

    def _init_stats(self):
        return {k: v.copy() for k, v in self.KEYS.items()}

    def get_scylla_versions(self):
        versions = {}
        try:
            node = self.db_cluster.nodes[0]
            if node.is_rhel_like():
                version_cmd = 'rpm -qa |grep scylla'
            else:
                version_cmd = "dpkg -l |grep scylla|awk '{print $2 \"-\" $3}'"
            versions_output = node.remoter.run(version_cmd).stdout.splitlines()
            for line in versions_output:
                for package in ['scylla-jmx', 'scylla-server', 'scylla-tools', 'scylla-enterprise-jmx',
                                'scylla-enterprise-server', 'scylla-enterprise-tools']:
                    match = re.search(r'(%s-(\S+)-(0.)?([0-9]{8,8}).(\w+).)' % package, line)
                    if match:
                        versions[package.replace('-enterprise', '')] = {'version': match.group(2),
                                                                        'date': match.group(4),
                                                                        'commit_id': match.group(5)}
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.error('Failed getting scylla versions: %s', ex)

        return versions

    def get_setup_details(self):
        exclude_details = ['send_email', 'email_recipients', 'es_url', 'es_password', 'reuse_cluster']
        setup_details = {}
        is_gce = self.params.get('cluster_backend') == 'gce'

        test_params = self.params.items()

        for key, value in test_params:
            if key in exclude_details or (isinstance(key, str) and key.startswith('stress_cmd')):  # pylint: disable=no-else-continue
                continue
            else:
                if is_gce and key in \
                        ['instance_type_loader',  # pylint: disable=no-else-continue
                         'instance_type_monitor',
                         'instance_type_db']:
                    # exclude these params from gce run
                    continue
                elif key == 'n_db_nodes' and isinstance(value, str) and re.search(r'\s', value):  # multidc
                    setup_details['n_db_nodes'] = sum([int(i) for i in value.split()])
                else:
                    setup_details[key] = value

        new_scylla_packages = self.params.get('update_db_packages')
        setup_details['packages_updated'] = bool(new_scylla_packages and os.listdir(new_scylla_packages))
        setup_details['cpu_platform'] = 'UNKNOWN'
        if is_gce and self.db_cluster:
            setup_details['cpu_platform'] = self.db_cluster.nodes[0]._instance.extra.get(  # pylint: disable=protected-access
                'cpuPlatform', 'UNKNOWN')

        setup_details['db_cluster_node_details'] = {}
        return setup_details

    def get_test_details(self):
        # avoid cyclic-decencies between cluster and db_stats
        from sdcm.cluster import Setup

        test_details = {}
        test_details['sct_git_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        test_details['job_name'] = get_job_name()
        test_details['job_url'] = os.environ.get('BUILD_URL', '')
        test_details['start_host'] = platform.node()
        test_details['test_duration'] = self.params.get(key='test_duration')
        test_details['start_time'] = time.time()
        test_details['grafana_snapshots'] = []
        test_details['grafana_screenshots'] = []
        test_details['grafana_annotations'] = []
        test_details['prometheus_data'] = ""
        test_details['test_id'] = Setup.test_id()
        test_details['log_files'] = {}
        return test_details

    def create_test_stats(self, sub_type=None, specific_tested_stats=None,  # pylint: disable=too-many-arguments
                          doc_id_with_timestamp=False, append_sub_test_to_name=True, test_name=None):

        if not self.create_stats:
            return
        self._test_index = self.__class__.__name__.lower()
        self._test_id = self._create_test_id(doc_id_with_timestamp)
        self._stats = self._init_stats()
        self._stats['setup_details'] = self.get_setup_details()
        self._stats['versions'] = self.get_scylla_versions()
        self._stats['test_details'] = self.get_test_details()
        test_name = test_name if test_name else self.id()
        if sub_type:
            self._stats['test_details']['sub_type'] = sub_type
        if sub_type and append_sub_test_to_name:
            self._stats['test_details']['test_name'] = '{}_{}'.format(test_name, sub_type)
        else:
            self._stats['test_details']['test_name'] = test_name
        for stat in self.PROMETHEUS_STATS:
            self._stats['results'][stat] = {}
        if specific_tested_stats:
            self._stats['results'].update(specific_tested_stats)
            self.log.info("Creating specific tested stats of: {}".format(specific_tested_stats))
        self.create()

    def update_stress_cmd_details(self, cmd, prefix='', stresser="cassandra-stress", aggregate=True):
        section = '{prefix}{stresser}'.format(prefix=prefix, stresser=stresser)
        if section not in self._stats['test_details']:
            self._stats['test_details'][section] = [] if aggregate else {}
        if stresser == "cassandra-stress":
            cmd_params = get_stress_cmd_params(cmd)
        elif stresser == "scylla-bench":
            cmd_params = get_stress_bench_cmd_params(cmd)
        elif stresser == 'ycsb':
            cmd_params = get_ycsb_cmd_params(cmd)
        elif stresser in ['gemini', 'ndbench']:
            cmd_params = get_raw_cmd_params(cmd)
        elif stresser == 'gemini':
            cmd_params = get_gemini_cmd_params(cmd)
        elif stresser == 'cdcreader':
            cmd_params = get_cdcreader_cmd_params(cmd)
        else:
            cmd_params = None
            self.log.warning("Unknown stresser: %s" % stresser)
        if cmd_params:
            if aggregate:
                self._stats['test_details'][section].append(cmd_params)
            else:
                self._stats['test_details'][section].update(cmd_params)
            self.update(dict(test_details=self._stats['test_details']))

    def _calc_stats(self, ps_results):
        try:
            if not ps_results or len(ps_results) <= 3:
                self.log.error("Not enough data from Prometheus: %s" % ps_results)
                return {}
            stat = {}
            ops_per_sec = [float(val) for _, val in ps_results if val.lower() != "nan"]  # float("nan") is not a number
            stat["max"] = max(ops_per_sec)
            # filter all values that are less than 1% of max
            ops_filtered = [x for x in ops_per_sec if x >= stat["max"] * 0.01]
            stat["min"] = min(ops_filtered)
            stat["avg"] = float(sum(ops_filtered)) / len(ops_filtered)
            stat["stdev"] = stddev(ops_filtered)
            self.log.debug("Stats: %s", stat)
            return stat
        except Exception as ex:  # pylint: disable=broad-except
            self.log.error("Exception when calculating PrometheusDB stats: %s" % ex)
            return {}

    @retrying(n=5, sleep_time=0, message="Retrying on getting prometheus stats")
    def get_prometheus_stats(self, alternator=False, scrap_metrics_step=None):
        self.log.info("Calculating throughput stats from PrometheusDB...")
        prometheus_db_stats = PrometheusDBStats(host=self.monitors.nodes[0].external_address,
                                                alternator=alternator)
        offset = 120  # 2 minutes offset
        start = int(self._stats["test_details"]["start_time"] + offset)
        end = int(time.time() - offset)
        prometheus_stats = {}
        for stat in self.PROMETHEUS_STATS:
            stat_calc_func = getattr(prometheus_db_stats, "get_" + stat)
            prometheus_stats[stat] = self._calc_stats(ps_results=stat_calc_func(start_time=start, end_time=end,
                                                                                scrap_metrics_step=scrap_metrics_step))
        self._stats['results'].update(prometheus_stats)
        return prometheus_stats

    def update_stress_results(self, results, calculate_stats=True):
        if 'stats' not in self._stats['results']:
            self._stats['results']['stats'] = results
        else:
            self._stats['results']['stats'].extend(results)
        if calculate_stats:
            self.calculate_stats_average()
            self.calculate_stats_total()
        self.update(dict(results=self._stats['results']))

    def _convert_stat(self, stat, stress_result):
        if stat not in stress_result or stress_result[stat] == 'NaN':
            self.log.warning("Stress stat not found: '%s'", stat)
            return 0
        try:
            return float(stress_result[stat])
        except Exception as details:  # pylint: disable=broad-except
            self.log.warning("Error in conversion of '%s' for stat '%s': '%s'"
                             "Discarding stat." % (stress_result[stat], stat, details))
        return 0

    def _calc_stat_total(self, stat):
        total = 0
        for stress_result in self._stats['results']['stats']:
            stat_val = self._convert_stat(stat=stat, stress_result=stress_result)
            if not stat_val:
                return 0  # discarding all stat results completely if one of the results is bad
            total += stat_val
        return total

    def calculate_stats_average(self):
        # calculate average stats
        average_stats = {}
        for stat in self.STRESS_STATS:
            average_stats[stat] = ''  # default
            total = self._calc_stat_total(stat=stat)
            if total:
                average_stats[stat] = round(total / len(self._stats['results']['stats']), 1)
        self._stats['results']['stats_average'] = average_stats

    def calculate_stats_total(self):
        total_stats = {}
        for stat in self.STRESS_STATS_TOTAL:
            total_stats[stat] = ''  # default
            total = self._calc_stat_total(stat=stat)
            if total:
                total_stats[stat] = total
        self._stats['results']['stats_total'] = total_stats

    def update_test_details(self, errors=None, coredumps=None, scylla_conf=False, extra_stats=None, alternator=False,
                            scrap_metrics_step=None):
        if not self.create_stats:
            return

        if not self._stats:
            self.log.error("Stats was not initialized. Could be error during Setup")
            return

        if extra_stats is None:
            extra_stats = {}

        update_data = {**extra_stats}
        update_data["test_details"] = test_details = self._stats.setdefault("test_details", {})
        update_data["setup_details"] = setup_details = self._stats.setdefault("setup_details", {})
        update_data["status"] = self._stats["status"] = self.status

        if errors:
            update_data["errors"] = errors

        if coredumps:
            update_data["coredumps"] = coredumps

        for key, value in extra_stats.items():
            self.log.debug("extra stats -- k: %s v: %s", key, value)
        self._stats["results"].update(extra_stats)

        test_details["time_completed"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

        if self.monitors and self.monitors.nodes:
            if self.params.get("store_perf_results"):
                update_data["results"] = self.get_prometheus_stats(alternator=alternator,
                                                                   scrap_metrics_step=scrap_metrics_step)
            grafana_dataset = self.monitors.get_grafana_screenshot_and_snapshot(test_details["start_time"])
            test_details.update({"grafana_screenshots": grafana_dataset.get("screenshots", []),
                                 "grafana_snapshots": grafana_dataset.get("snapshots", []),
                                 "grafana_annotations": self.monitors.upload_annotations_to_s3(),
                                 "prometheus_data": self.monitors.download_monitor_data(), })

        if self.db_cluster and self.db_cluster.nodes:
            node = self.db_cluster.nodes[0]

            def output(cmd):
                result = node.remoter.sudo(cmd, ignore_status=True, verbose=True)
                if result.ok:
                    return result.stdout.strip()
                self.log.error("Failed to run `%s' on %s", cmd, node)
                return "<< failed to get >>"

            setup_details["db_cluster_node_details"] = {
                "cpu_model": output("awk -F: '/^model name/{print $2; exit}' /proc/cpuinfo"),
                "sys_info": output("uname -a"),
            }
            if scylla_conf and "scylla_args" not in setup_details:
                scylla_server_conf = f"/etc/{'sysconfig' if node.distro.is_rhel_like else 'default'}/scylla-server"
                setup_details.update({"scylla_args": output(f"grep ^SCYLLA_ARGS {scylla_server_conf}"),
                                      "io_conf": output("grep -v ^# /etc/scylla.d/io.conf"),
                                      "cpuset_conf": output("grep -v ^# /etc/scylla.d/cpuset.conf"), })

        self.update(update_data)

    def get_doc_data(self, key):
        if self.create_stats and self._test_index and self.get_doc_id():
            result = self.elasticsearch.get_doc(self._test_index, self.get_doc_id(), doc_type=self._es_doc_type)

            return result['_source'].get(key, None)
        return None

    def get_test_start_time(self):
        return self._stats['test_details'].get('start_time', None)
