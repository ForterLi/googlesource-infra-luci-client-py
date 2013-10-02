#!/usr/bin/env python
# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Uploads a ton of stuff to isolateserver to test its handling.

Generates an histogram with the latencies to download a just uploaded file.

Note that it only looks at uploading and downloading and do not test
/content/contains, which is datastore read bound.
"""

import functools
import hashlib
import json
import logging
import optparse
import os
import random
import sys
import time
import zlib

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT_DIR)

from third_party import colorama

import isolateserver

from utils import graph
from utils import threading_utils


class Randomness(object):
  def __init__(self, random_pool_size=1024):
    """Creates 1mb of random data in a pool in 1kb chunks."""
    self.pool = [
      ''.join(chr(random.randrange(256)) for _ in xrange(1024))
      for _ in xrange(random_pool_size)
    ]

  def gen(self, size):
    """Returns a str containing random data from the pool of size |size|."""
    chunks = int(size / 1024)
    rest = size - (chunks*1024)
    data = ''.join(random.choice(self.pool) for _ in xrange(chunks))
    data += random.choice(self.pool)[:rest]
    return data


class Progress(threading_utils.Progress):
  def _render_columns(self):
    """Prints the size data as 'units'."""
    columns_as_str = [
        str(self._columns[0]),
        graph.to_units(self._columns[1]).rjust(6),
        str(self._columns[2]),
    ]
    max_len = max((len(columns_as_str[0]), len(columns_as_str[2])))
    return '/'.join(i.rjust(max_len) for i in columns_as_str)


def print_results(results, columns, buckets):
  delays = [i[0] for i in results if isinstance(i[0], float)]
  failures = [i for i in results if not isinstance(i[0], float)]
  sizes = [i[1] for i in results]

  print('%sSIZES%s (bytes):' % (colorama.Fore.RED, colorama.Fore.RESET))
  graph.print_histogram(
      graph.generate_histogram(sizes, buckets), columns, '%d')
  print('')
  total_size = sum(sizes)
  print('Total size  : %s' % graph.to_units(total_size))
  print('Total items : %d' % len(sizes))
  print('Average size: %s' % graph.to_units(total_size / len(sizes)))
  print('Largest item: %s' % graph.to_units(max(sizes)))
  print('')
  print('%sDELAYS%s (seconds):' % (colorama.Fore.RED, colorama.Fore.RESET))
  graph.print_histogram(
      graph.generate_histogram(delays, buckets), columns, '%.3f')

  if failures:
    print('')
    print('%sFAILURES%s:' % (colorama.Fore.RED, colorama.Fore.RESET))
    print(
        '\n'.join('  %s (%s)' % (i[0], graph.to_units(i[1])) for i in failures))


def gen_size(mid_size):
  """Interesting non-guassian distribution, to get a few very large files.

  Found via guessing on Wikipedia. Module 'random' says it's threadsafe.
  """
  return int(random.gammavariate(3, 2) * mid_size / 4)


def send_and_receive(random_pool, dry_run, api, progress, size):
  """Sends a random file and gets it back.

  Returns (delay, size)
  """
  # Create a file out of the pool.
  start = time.time()
  content = random_pool.gen(size)
  compressed_content = zlib.compress(content, 0)
  hash_value = hashlib.sha1(content).hexdigest()
  size = len(content)
  try:
    if not dry_run:
      logging.info('contains')
      item = isolateserver.Item(hash_value, size)
      item = api.contains([item])[0]

      logging.info('upload')
      # TODO(maruel): There's currently a mismatch between push() and fetch(),
      # push() gets the data pre-compressed but fetch() decompresses on the fly.
      api.push(item, [compressed_content], len(compressed_content))

      logging.info('download')
      # Only count download time when not in dry run time.
      # TODO(maruel): Count the number of retries!
      start = time.time()
      assert content == ''.join(api.fetch(hash_value, size))
    else:
      time.sleep(size / 10.)
    duration = max(0, time.time() - start)
  except isolateserver.MappingError as e:
    duration = str(e)
  if isinstance(duration, float):
    progress.update_item('', index=1, data=size)
  else:
    progress.update_item('', index=1)
  return (duration, size)


def main():
  colorama.init()

  parser = optparse.OptionParser(description=sys.modules[__name__].__doc__)
  parser.add_option(
      '-I', '--isolate-server',
      metavar='URL', default='',
      help='Isolate server to use')
  parser.add_option(
      '--namespace', default='temporary%d-gzip' % time.time(), metavar='XX',
      help='Namespace to use on the server, default: %default')
  parser.add_option(
      '--threads', type='int', default=16, metavar='N',
      help='Parallel worker threads to use, default:%default')
  graph.unit_option(
      parser, '--items', default=0, help='Number of items to upload')
  graph.unit_option(
      parser, '--max-size', default=0,
      help='Loop until this amount of data was transferred')
  graph.unit_option(
      parser, '--mid-size', default=100*1024,
      help='Rough average size of each item, default:%default')
  parser.add_option(
      '--columns', type='int', default=graph.get_console_width(), metavar='N',
      help='For histogram display, default:%default')
  parser.add_option(
      '--buckets', type='int', default=20, metavar='N',
      help='Number of buckets for histogram display, default:%default')
  parser.add_option(
      '--dump', metavar='FOO.JSON', help='Dumps to json file')
  parser.add_option(
      '--dry-run', action='store_true', help='Do not send anything')
  parser.add_option(
      '-v', '--verbose', action='store_true', help='Enable logging')
  options, args = parser.parse_args()

  logging.basicConfig(level=logging.INFO if options.verbose else logging.FATAL)
  if args:
    parser.error('Unsupported args: %s' % args)
  if bool(options.max_size) == bool(options.items):
    parser.error(
        'Use one of --max-size or --items.\n'
        '  Use --max-size if you want to run it until NN bytes where '
        'transfered.\n'
        '  Otherwise use --items to run it for NN items.')
  if not options.dry_run:
    options.isolate_server = options.isolate_server.rstrip('/')
    if not options.isolate_server:
      parser.error('--isolate-server is required.')

  print(
      ' - Using %d thread,  items=%d,  max-size=%d,  mid-size=%d' % (
      options.threads, options.items, options.max_size, options.mid_size))
  if options.dry_run:
    print(' - %sDRY RUN MODE%s' % (colorama.Fore.GREEN, colorama.Fore.RESET))

  start = time.time()

  random_pool = Randomness()
  print(' - Generated pool after %.1fs' % (time.time() - start))

  columns = [('index', 0), ('data', 0), ('size', options.items)]
  progress = Progress(columns)
  api = isolateserver.get_storage_api(options.isolate_server, options.namespace)
  do_item = functools.partial(
      send_and_receive,
      random_pool,
      options.dry_run,
      api,
      progress)

  # TODO(maruel): Handle Ctrl-C should:
  # - Stop adding tasks.
  # - Stop scheduling tasks in ThreadPool.
  # - Wait for the remaining ungoing tasks to complete.
  # - Still print details and write the json file.
  with threading_utils.ThreadPoolWithProgress(
      progress, options.threads, options.threads, 0) as pool:
    if options.items:
      for _ in xrange(options.items):
        pool.add_task(0, do_item, gen_size(options.mid_size))
        progress.print_update()
    elif options.max_size:
      # This one is approximate.
      total = 0
      while True:
        size = gen_size(options.mid_size)
        progress.update_item('', size=1)
        progress.print_update()
        pool.add_task(0, do_item, size)
        total += size
        if total >= options.max_size:
          break
    results = sorted(pool.join())

  print('')
  print(' - Took %.1fs.' % (time.time() - start))
  print('')
  print_results(results, options.columns, options.buckets)
  if options.dump:
    with open(options.dump, 'w') as f:
      json.dump(results, f, separators=(',',':'))
  return 0


if __name__ == '__main__':
  sys.exit(main())