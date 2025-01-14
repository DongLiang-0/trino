#!/usr/bin/env python3

import argparse
import collections
import copy
import itertools
import yaml
import json
import logging
import subprocess
import sys
import tempfile
import unittest


def main():
    parser = argparse.ArgumentParser(
        description="Filter test matrix modules using list of impacted features."
    )
    parser.add_argument(
        "-m",
        "--matrix",
        type=argparse.FileType("r"),
        default=".github/test-pt-matrix.yaml",
        help="A YAML file with the PT matrix",
    )
    parser.add_argument(
        "-i",
        "--impacted-features",
        type=argparse.FileType("r"),
        dest="impacted_features",
        default="impacted-features.log",
        help="List of impacted features, one per line",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=argparse.FileType("w"),
        default=sys.stdout,
        help="Filename to write JSON output to",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Print info level logs",
    )
    parser.add_argument(
        "-t",
        "--test",
        action="store_true",
        help="test this script instead of executing it",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=args.loglevel, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.test:
        sys.argv = [sys.argv[0]]
        unittest.main()
        return
    build(args.matrix, args.impacted_features, args.output, "testing/bin/ptl")


def excluded(item, excludes):
    result = any(exclude.items() <= item.items() for exclude in excludes)
    logging.debug("excluded(%s, %s) returns %s", item, excludes, result)
    return result


def expand_matrix(matrix):
    include = matrix.pop("include", [])
    exclude = matrix.pop("exclude", [])

    # for every key in the matrix dict, convert its values to tuples of key and value
    tuples = [[(k, v) for v in vals] for k, vals in matrix.items()]
    logging.debug("tuples: %s", tuples)
    # then calculate the product of such lists of tuples
    # and convert tuples back to a dict stored in a list
    combinations = list(map(dict, itertools.product(*tuples)))
    logging.debug("combinations: %s", combinations)
    # filter out excludes and add includes as the last step
    # so that excluded combinations can be explicitly added back
    return [item for item in combinations if not excluded(item, exclude)] + include


def load_available_features_for_config(config, suites, ptl_binary_path):
    cmd = [
        ptl_binary_path,
        "suite",
        "describe",
        "--suite",
        ",".join(suites),
        "--config",
        "config-" + config,
        "--format",
        "JSON",
    ]
    logging.debug("executing: %s", " ".join(cmd))
    process = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
    )
    logging.debug("ptl suite describe: %s", process)
    if process.returncode != 0:
        logging.error("ptl suite describe failed: %s", process)
        return {}
    for line in process.stdout.splitlines():
        if line.startswith("{"):
            logging.debug("Parsing JSON: %s", line)
            ptl_output = json.loads(line)
            logging.debug("Handling JSON object: %s", ptl_output)
            config_features = {}
            for suite in ptl_output.get("suites", []):
                key = (config, suite.get("name"))
                value = set()
                for testRun in suite.get("testRuns", []):
                    for connector in testRun["environment"].get("features", []):
                        value.add(connector)
                config_features[key] = value

            logging.debug("config_features: %s", config_features)
            return config_features
    logging.error("ptl suite describe hasn't returned any JSON line: %s", process)
    return {}


def load_available_features(configToSuiteMap, ptl_binary_path):
    available_connectors = {}
    for config, suites in configToSuiteMap.items():
        available_connectors.update(
            load_available_features_for_config(config, suites, ptl_binary_path)
        )
    return available_connectors


def tested_features(available_connectors, config, suite):
    return available_connectors.get((config, suite), [])


def build(matrix_file, impacted_file, output_file, ptl_binary_path):
    matrix = yaml.load(matrix_file, Loader=yaml.Loader)
    impacted_features = list(
        filter(None, [line.rstrip() for line in impacted_file.readlines()])
    )
    logging.info("Read matrix: %s", matrix)
    logging.info("Read impacted_features: %s", impacted_features)
    result = copy.copy(matrix)
    items = expand_matrix(matrix)
    logging.info("Expanded matrix: %s", items)

    configToSuiteMap = collections.defaultdict(list)
    for item in items:
        configToSuiteMap[item.get("config")].append(item.get("suite"))
    available_features = load_available_features(configToSuiteMap, ptl_binary_path)
    if len(available_features) > 0:
        for item in items:
            features = tested_features(
                available_features, item.get("config"), item.get("suite")
            )
            logging.debug(
                "impacted_features: %s, features: %s", impacted_features, features
            )
            if not any(connector in impacted_features for connector in features):
                logging.info("Excluding matrix entry due to features: %s", item)
                result.setdefault("exclude", []).append(item)
                if "include" in result and item in result["include"]:
                    logging.debug("Removing from include list: %s", item)
                    result["include"].remove(item)
    json.dump(result, output_file)
    output_file.write("\n")


class TestBuild(unittest.TestCase):
    def test_build(self):
        self.maxDiff = None
        cases = [
            # missing features get added to exclude list
            (
                {
                    "config": ["A", "B", "C"],
                    "suite": ["1", "2", "3"],
                },
                ["A:1", "B:2", "C:1", "C:3", "D:1"],
                ".github/bin/fake-ptl",
                {
                    "config": ["A", "B", "C"],
                    "suite": ["1", "2", "3"],
                    "exclude": [
                        {"config": "A", "suite": "2"},
                        {"config": "A", "suite": "3"},
                        {"config": "B", "suite": "1"},
                        {"config": "B", "suite": "3"},
                        {"config": "C", "suite": "2"},
                    ],
                },
            ),
            # missing features get removed from include list
            (
                {
                    "config": ["A", "B", "C"],
                    "suite": ["1", "2", "3"],
                    "include": [
                        {"config": "A", "suite": "4"},
                        {"config": "D", "suite": "1"},
                        {"config": "D", "suite": "2"},
                    ],
                },
                ["A:1", "B:2", "C:1", "C:3", "D:1"],
                ".github/bin/fake-ptl",
                {
                    "config": ["A", "B", "C"],
                    "suite": ["1", "2", "3"],
                    "exclude": [
                        {"config": "A", "suite": "2"},
                        {"config": "A", "suite": "3"},
                        {"config": "B", "suite": "1"},
                        {"config": "B", "suite": "3"},
                        {"config": "C", "suite": "2"},
                        {"config": "A", "suite": "4"},
                        {"config": "D", "suite": "2"},
                    ],
                    "include": [
                        {"config": "D", "suite": "1"},
                    ],
                },
            ),
            # missing features get added to exclude list and removed from include
            (
                {
                    "config": ["A", "B", "C"],
                    "suite": ["1", "2", "3"],
                    "exclude": [
                        {"config": "A", "suite": "1"},
                        {"config": "A", "suite": "2"},
                    ],
                    "include": [
                        {"config": "A", "suite": "1", "jdk": "17"},
                        {"config": "D", "suite": "1"},
                        {"config": "D", "suite": "2"},
                    ],
                },
                ["A:1", "B:2", "C:1", "C:3", "D:1"],
                ".github/bin/fake-ptl",
                {
                    "config": ["A", "B", "C"],
                    "suite": ["1", "2", "3"],
                    "exclude": [
                        {"config": "A", "suite": "1"},
                        {"config": "A", "suite": "2"},
                        {"config": "A", "suite": "3"},
                        {"config": "B", "suite": "1"},
                        {"config": "B", "suite": "3"},
                        {"config": "C", "suite": "2"},
                        {"config": "D", "suite": "2"},
                    ],
                    "include": [
                        {"config": "A", "suite": "1", "jdk": "17"},
                        {"config": "D", "suite": "1"},
                    ],
                },
            ),
            # integration test with real PTL
            (
                # input matrix
                {
                    "config": ["default", "hdp3", "cdh5"],
                    "suite": ["suite-1", "suite-2", "suite-3", "suite-5"],
                    "jdk": ["11"],
                    "ignore exclusion if": [False],
                    "exclude": [
                        {"config": "cdh5", "ignore exclusion if": True},
                        {"config": "default", "ignore exclusion if": False},
                    ],
                    "include": [
                        {
                            "config": "default",
                            "suite": "suite-6-non-generic",
                            "jdk": "11",
                        },
                        {
                            "config": "default",
                            "suite": "suite-7-non-generic",
                            "jdk": "11",
                        },
                        {
                            "config": "default",
                            "suite": "suite-8-non-generic",
                            "jdk": "11",
                        },
                        {"config": "default", "suite": "suite-tpcds", "jdk": "11"},
                        {"config": "default", "suite": "suite-oauth2", "jdk": "11"},
                        {"config": "default", "suite": "suite-ldap", "jdk": "11"},
                        {
                            "config": "default",
                            "suite": "suite-compatibility",
                            "jdk": "11",
                        },
                        {
                            "config": "apache-hive3",
                            "suite": "suite-hms-only",
                            "jdk": "11",
                        },
                        {"config": "hdp3", "suite": "suite-1", "jdk": "17"},
                        {"config": "hdp3", "suite": "suite-2", "jdk": "17"},
                    ],
                },
                # impacted features
                ["connector:hive"],
                # ptl_binary_path
                "testing/bin/ptl",
                # expected matrix
                {
                    "config": ["default", "hdp3", "cdh5"],
                    "exclude": [
                        {"config": "cdh5", "ignore exclusion if": True},
                        {"config": "default", "ignore exclusion if": False},
                        {"config": "default", "jdk": "11", "suite": "suite-oauth2"},
                    ],
                    "ignore exclusion if": [False],
                    "include": [
                        {
                            "config": "default",
                            "jdk": "11",
                            "suite": "suite-6-non-generic",
                        },
                        {
                            "config": "default",
                            "jdk": "11",
                            "suite": "suite-7-non-generic",
                        },
                        {
                            "config": "default",
                            "jdk": "11",
                            "suite": "suite-8-non-generic",
                        },
                        {"config": "default", "jdk": "11", "suite": "suite-tpcds"},
                        {"config": "default", "jdk": "11", "suite": "suite-ldap"},
                        {
                            "config": "default",
                            "jdk": "11",
                            "suite": "suite-compatibility",
                        },
                        {
                            "config": "apache-hive3",
                            "jdk": "11",
                            "suite": "suite-hms-only",
                        },
                        {"config": "hdp3", "jdk": "17", "suite": "suite-1"},
                        {"config": "hdp3", "jdk": "17", "suite": "suite-2"},
                    ],
                    "jdk": ["11"],
                    "suite": ["suite-1", "suite-2", "suite-3", "suite-5"],
                },
            ),
        ]
        for matrix, impacted, ptl_binary_path, expected in cases:
            with self.subTest():
                with tempfile.TemporaryFile(
                    "w+"
                ) as matrix_file, tempfile.TemporaryFile(
                    "w+"
                ) as impacted_file, tempfile.TemporaryFile(
                    "w+"
                ) as output_file:
                    # given
                    yaml.dump(matrix, matrix_file)
                    matrix_file.seek(0)
                    impacted_file.write("\n".join(impacted))
                    impacted_file.seek(0)
                    # when
                    build(matrix_file, impacted_file, output_file, ptl_binary_path)
                    output_file.seek(0)
                    output = json.load(output_file)
                    # then
                    self.assertEqual(output, expected)


if __name__ == "__main__":
    main()
