#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Author: marirs
Description: Look at files in a given path for yara rules, and dedupe them based on rule name
Version: 0.2
Requirements: python 3.5 & yara-python, re2 (for regex performance)
Changelog: 0.1: initial commit
Changelog: 0.2: Porting to Python 3.5. Multi-threading option added; single-threaded by default.
"""

import argparse
import datetime
import io
import os
import sys
import threading
import time
from itertools import groupby

try:
    import re2 as re
except ImportError:
    import re

try:
    import yara

    YARAMODULE = True
except ImportError:
    YARAMODULE = False

sys.dont_write_bytecode = True

__version__ = 0.2
__author__ = "marirs@gmail.com"
__license__ = "GPL"
__file__ = "dedupe_yara_rule.py"

all_imports = set()
all_yara_rules = set()
rule_names = set()
total_duplicate_rules = 0
total_rules = 0
total_rules_written = 0
rule_dict = {}
yara_rule_regex = r"(^[\s+private\/\*]*rule\s[0-9a-zA-Z_\@\#\$\%\^\&\(\)\-\=\:\s]+\{.*?condition.*?\s\})"
comments_regex = r"(/\*([^*]|[\r\n]|(\*+([^*/]|[\r\n])))*\*+/|^//.*?$)"
imports_regex = r"(^import\s+.*?$)"
rules_re = re.compile(yara_rule_regex, re.MULTILINE | re.DOTALL)
import_re = re.compile(imports_regex, re.MULTILINE | re.DOTALL)
comments_re = re.compile(comments_regex, re.MULTILINE | re.DOTALL)
__spin_threads__ = 10  # threads to create for processing
lock = threading.Lock()
verbose = False


#
# threading worker class
#
class ThreadWorker(threading.Thread):
    """
    desc: threading class for certain functions that
    returns value. can be used with functions that don't return value as well
    input: function, function arguments
    return:
    """

    def __init__(self, *args, **kwargs):
        super(ThreadWorker, self).__init__(*args, **kwargs)

        self._return = None

    def run(self):
        if self._target is not None:
            self._return = self._target(*self._args, **self._kwargs)

    def join(self, *args, **kwargs):
        super(ThreadWorker, self).join(*args, **kwargs)

        return self._return


def chk_yara_import(Import):
    """
    Checks if the yara module exists or not!
    :param Import: yara import
    :return: returns true if exists else false
    """
    try:
        yara.compile(source=Import)
    except:
        return False

    return True


def write_file(filepath, contents):
    """
    Write contents to file
    :param filepath: filepath + filename
    :param contents: contents that needs to be written (type: list or unicode string)
    :return: none
    """
    if type(contents) == list:
        contents = [_f for _f in [x.strip() for x in list(contents)] if _f]
        contents = "\n\n".join(contents)
    with io.open(filepath, 'w', encoding='utf-8') as f:
        try:
            f.write(str("/* file generated by yara deduper {} @ {} */\n\n".format(__version__, time.strftime(
                "%h %d, %Y %I:%M:%S %p %Z"))))
            f.write(contents)
        except Exception as err:
            sys.stdout.write("\n[!] Error writing {}: {}".format(filepath, err))


def extract(yara_file):
    """
    Extracts rules, commented rules and imports from a given yara file
    :param yara_file: Yara file
    :return: tuple (list of imports/None, list of yara rules/None, list of commented yara rules/None)
    """
    content = None
    yara_rules = []
    commented_yar_rules = []
    imports = []
    result_tuple = None
    encodings = ['utf-8', 'cp1252', 'windows-1250', 'windows-1252', 'ascii']
    for e in encodings:
        sys.stdout.flush()
        with io.open(yara_file, "r", encoding=e) as rule_file:
            # Read from rule file
            try:
                content = rule_file.read()
                break
            except Exception as err:
                sys.stdout.write("\n[!] {}: {}".format(yara_file, err))
                if encodings.index(e) + 1 < len(encodings):
                    sys.stdout.write(
                        "\n -> trying codec: {} for {}".format(encodings[encodings.index(e) + 1], yara_file))
                else:
                    sys.stdout.write("\n[!] No codec matched to open {}".format(yara_file))
                content = None

    if not content:
        return (None, None, None)

    yara_rules = rules_re.findall(content)
    if verbose:
        sys.stdout.write("\n[{:>5} rules] {}".format(len(yara_rules), yara_file))
        sys.stdout.flush()

    if yara_rules:
        # clean 'em
        yara_rules = [rule.strip().strip("*/").strip().strip("/*").strip() for rule in yara_rules]
        # we have some yara rules in this file
        # lets check for comments or commented rules & the imports
        # in this rule file
        imports = import_re.findall(content)
        commented_yar_rules = comments_re.findall(content)

        if commented_yar_rules:
            commented_yar_rules = [_f for _f in [comments for sub in commented_yar_rules for comments in sub if
                                                 comments.strip().startswith(("/*", "//"))] if _f]
            # remove commented yara rules
            yara_rules = [x for x in yara_rules if x not in "".join(commented_yar_rules)]

    result_tuple = (imports, yara_rules, commented_yar_rules)
    return result_tuple


def dedupe(yara_files, yara_output_path):
    """
    dedupe yara rules and store the unique ones in the output directory
    :param yara_rules_path: path to where yara rules are present
    :param yara_output_path: path to where deduped yara rules are written
    :return:
    """
    global total_duplicate_rules
    global rule_names
    global all_imports
    global all_yara_rules
    global rule_dict
    global total_rules
    yara_deduped_rules_path = os.path.join(yara_output_path, "deduped_rules")
    yara_commeted_rules_path = os.path.join(yara_output_path, "commented_rules")

    # go over all the yara rule files and process them
    for yf in yara_files:
        sys.stdout.flush()
        deduped_content = ""
        yf_rule_dir = os.path.basename(os.path.normpath(os.path.dirname(yf)))
        new_yf_rule_dir = os.path.join(yara_deduped_rules_path, yf_rule_dir)
        new_yf_commented_rule_dir = os.path.join(yara_commeted_rules_path, yf_rule_dir)
        yf_file_name = os.path.basename(yf)

        imports, yar_rules, commented_yar_rules = extract(yf)
        if not imports and not yar_rules and not commented_yar_rules:
            if verbose:
                sys.stdout.write("\n[{:>5} rules] {}".format(0, yf_file_name))
                sys.stdout.flush()

            continue

        if imports:
            # we found some imports
            lock.acquire()
            all_imports.update(imports)
            lock.release()

            deduped_content = "".join(imports) + "\n" * 3

        if commented_yar_rules:
            # commented rules found
            if not os.path.exists(new_yf_commented_rule_dir):
                os.mkdir(new_yf_commented_rule_dir)

            commented_yar_rules = imports + commented_yar_rules if imports else commented_yar_rules
            # write the commented rules to file
            write_file(os.path.join(new_yf_commented_rule_dir, yf_file_name), commented_yar_rules)

        if yar_rules:
            lock.acquire()
            total_rules += len(yar_rules)
            if not os.path.isdir(new_yf_rule_dir):
                os.mkdir(new_yf_rule_dir)
            lock.release()

            for r in yar_rules:
                rulename = r.strip().splitlines()[0].strip().partition("{")[0].strip()
                rulename = r.split(":")[0].strip() if ":" in rulename else rulename
                lock.acquire()
                rule_dict[rulename] = rule_dict.get(rulename, [])
                rule_dict[rulename].append(yf)

                if rulename not in rule_names:
                    deduped_content += "".join(r.strip()) + "\n" * 2
                    rule_names.update([rulename])
                    all_yara_rules.update(["\n// rule from: {}\n".format(yf) + r.strip() + "\n"])
                else:
                    total_duplicate_rules += 1
                lock.release()

            # write the deduped rule to file
            write_file(os.path.join(new_yf_rule_dir, yf_file_name), str(deduped_content))


def dedupe_serial():
    try:
        dedupe(yara_files, args.out)
    except KeyboardInterrupt:
        exit("\n[!] ^C break!")


def dedupe_threaded():
    global __spin_threads__, yara_files
    total_items = len(yara_files)
    if total_items <= __spin_threads__:
        __spin_threads__ = total_items
    each_thread_items_count = total_items // __spin_threads__
    extra_items_count = total_items % __spin_threads__
    yara_files = [yara_files[i:i + each_thread_items_count] for i in
                  range(0, len(yara_files), each_thread_items_count)]
    __spin_threads__ = len(yara_files)
    threads = []
    sys.stdout.write("\n[*] threads: {} | each thread: {} files, extra thread: {} files".format(
        __spin_threads__, each_thread_items_count, extra_items_count))

    sys.stdout.write("\n[*] processing files...")
    sys.stdout.flush()

    for i in range(__spin_threads__):
        t = ThreadWorker(target=dedupe, args=(yara_files[i], args.out,))
        t.daemon = True
        t.start()
        threads.append(t)
    # yield - wait for the threads
    # to complete their job
    for each_thread in threads:
        try:
            each_thread.join()
        except TypeError:
            sys.stdout.write("{} completed.".format(each_thread))
        except KeyboardInterrupt:
            exit("\n[!] ^C break!")


if __name__ == "__main__":
    """
    script begins :)
    """
    start_time = time.time()
    sys.stdout.write("Yara Rules deduper v{}".format(__version__))
    sys.stdout.write("\nmarirs (at) gmail.com / Licence: GPL")
    sys.stdout.write("\nDisclaimer: This script is provided as-is without any warranty. Use at your own Risk :)")
    sys.stdout.write("\nReport bugs/issues at: https://github.com/marirs/dedupe_yara_rule/issues\n")
    if not YARAMODULE:
        sys.stdout.write(
            "\n[!] yara-python is not installed, will skip rule verification & module import check!")

    parser = argparse.ArgumentParser(description='dedupe yara rules')
    parser.add_argument('-p', '--path', help='yara rules path', required=True)
    parser.add_argument('-o', '--out', default='./yara_new', help="output directory")
    parser.add_argument('-v', '--verbose', action="store_true", help="increase output verbosity")
    parser.add_argument('-t', '--threaded', action="store_true", help="run multi-threaded")
    args = parser.parse_args()

    if args.path:
        if not os.path.exists(args.path):
            exit("[!] {} does not exist. a valid path to yara rules is required!".format(args.path))

    if args.out:
        misc_folders = ["commented_rules", "deduped_rules"]
        if not os.path.exists(args.out):
            try:
                os.mkdir(args.out)
            except:
                exit("[!] output directory does not exist and could not be created ({})".format(args.out))

        for f in misc_folders:
            if not os.path.exists(os.path.join(args.out, f)):
                os.mkdir(os.path.join(args.out, f))
            sys.stdout.write("\n[*] '{}' set to be the output directory!".format(
                os.path.join(os.getcwd(), str(args.out).replace("./", "")) if "./" in args.out else args.out))

    if args.verbose:
        verbose = True

    file_types = (".yar", ".yara")
    yara_files = [os.path.join(root, f) for root, dir, files in os.walk(args.path) for f in files if
                  f.endswith(file_types)]
    if not yara_files:
        exit("[!] 0 yara files to process from '{}'!".format(args.path))

        sys.stdout.write("\n[*] Total files to process: {} files in {} dirs.".format(
            len(yara_files),
            len(set([os.path.basename(os.path.normpath(os.path.dirname(f))) for f in yara_files])))
        )

    if args.threaded:
        dedupe_threaded()
    else:
        dedupe_serial()

    # post dedupe
    sys.stdout.write("\n" + "-" * 35)
    sys.stdout.write("\n[*] Duplicate rules:")
    for key, value in list(rule_dict.items()):
        if len(value) > 1:
            sys.stdout.write("\n -> \"{}\" in {}".format(key, ", ".join(value)))

    total_rules_written = len(all_yara_rules)
    sys.stdout.write("\n" + "-" * 10)
    sys.stdout.write(
        "\n[*] Total Rules: {}\n[*] Total Rules after dedupe: {}\n[*] Total Duplicate Rules: {}".format(total_rules,
                                                                                                        total_rules_written,
                                                                                                        total_duplicate_rules))

    yara_deduped_rules_path = os.path.join(args.out, "deduped_rules")
    # Merge all the yara rules
    all_yara_rules = "\n".join(list(all_imports)) + "\n" * 2 + "\n\n".join(list(sorted(all_yara_rules)))
    # write all the deduped rules into 1 single file
    write_file(os.path.join(yara_deduped_rules_path, "all_in_one.yar"), all_yara_rules)

    if YARAMODULE and all_imports:
        # check if imports are available or not
        sys.stdout.write("\n" + "-" * 35)
        sys.stdout.write("\n[*] Checking yara import modules...")
        for module in all_imports:
            sys.stdout.write(
                "\n -> {}: {}".format(module, "You dont have this module!" if not chk_yara_import(module) else "PASS"))

    sys.stdout.write("\n" + "-" * 35)
    # write index files
    sys.stdout.write("\n[*] Creating index files...")
    sys.stdout.write("\n -> {}".format(os.path.join(yara_deduped_rules_path, "all_in_one_rules.yar")))
    yara_files = [os.path.join(root, f) for root, dir, files in os.walk(yara_deduped_rules_path) for f in
                  files if "all_in_one_rules.yar" not in f and f.endswith(file_types)]
    index_files = str("\n".join(["include \"{}\"".format(f) for f in yara_files]))
    write_file(os.path.join(yara_deduped_rules_path, "index.yar"), index_files)
    sys.stdout.write("\n -> {}".format(os.path.join(yara_deduped_rules_path, "index.yar")))
    # individual index files
    index_files = [(os.path.dirname(fp), os.path.basename(fp)) for fp in yara_files]
    for key, group in groupby(index_files, lambda x: x[0]):
        list_of_files = str("\n".join(["include \"{}/{}\"".format(file[0], file[1]) for file in group]))
        fname = os.path.basename(key) + "_index.yar"
        write_file(os.path.join(yara_deduped_rules_path, fname), list_of_files)
        sys.stdout.write("\n -> {}".format(os.path.join(yara_deduped_rules_path, fname)))
    sys.stdout.write("\n" + "-" * 35)

    if YARAMODULE:
        # compile for rule errors
        sys.stdout.write("\n[*] Verifying rules...")

        for file in yara_files:
            try:
                # verify the rule file
                yara.compile(file)
            except Exception as err:
                sys.stdout.write("\n -> {} [skipped file due to compile error...]".format(err))
        sys.stdout.write("\n" + "-" * 35)

    execution_time = time.time() - start_time
    sys.stdout.write("\n[*] All rules organised in {}".format(
        os.path.join(os.getcwd(), str(args.out).replace("./", "")) if "./" in args.out else args.out))
    sys.stdout.write("\n[*] Time taken: {}".format(datetime.timedelta(seconds=execution_time)))
    sys.stdout.write("\n" + "-" * 15 + "\n")
