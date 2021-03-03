#!/usr/bin/env vpython3
# Copyright 2013 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

from __future__ import print_function

import base64
import hashlib
import json
import logging
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zlib

import mock
import parameterized
import six

# Mutates sys.path.
import test_env

import isolateserver_fake
import net_utils

import auth
import isolated_format
import isolate_storage
import isolateserver
import local_caching
from utils import file_path
from utils import fs
from utils import logging_utils
from utils import threading_utils


CONTENTS = {
    'empty_file.txt': b'',
    'small_file.txt': b'small file\n',
    # TODO(maruel): symlinks.
}


class TestCase(net_utils.TestCase):
  # These tests fail when running with other tests
  # Need to run in test_seq.py
  no_run = 1

  """Mocks out url_open() calls and sys.stdout/stderr."""
  _tempdir = None

  def setUp(self):
    super(TestCase, self).setUp()
    self.mock(auth, 'ensure_logged_in', lambda _: None)
    self.old_cwd = os.getcwd()
    self.mock_print = mock.patch('six.moves.builtins.print').start()

  def tearDown(self):
    mock.patch.stopall()
    try:
      os.chdir(self.old_cwd)
      if self._tempdir:
        file_path.rmtree(self._tempdir)
      self.checkOutput([])
    finally:
      super(TestCase, self).tearDown()

  @property
  def tempdir(self):
    if not self._tempdir:
      self._tempdir = tempfile.mkdtemp(prefix=u'isolateserver')
    return self._tempdir

  def make_tree(self, contents):
    test_env.make_tree(self.tempdir, contents)

  def checkOutput(self, expected_out):
    self.assertEqual(self.mock_print.call_args_list, expected_out)
    self.mock_print.reset_mock()


class TestZipCompression(TestCase):
  """Test zip_compress and zip_decompress generators."""

  def test_compress_and_decompress(self):
    """Test data === decompress(compress(data))."""
    original = [str(x).encode() for x in range(0, 1000)]
    processed = isolateserver.zip_decompress(
        isolateserver.zip_compress(original))
    self.assertEqual(b''.join(original), b''.join(processed))

  def test_zip_bomb(self):
    """Verify zip_decompress always returns small chunks."""
    original = b'\x00' * 100000
    bomb = b''.join(isolateserver.zip_compress([original]))
    decompressed = []
    chunk_size = 1000
    for chunk in isolateserver.zip_decompress([bomb], chunk_size):
      self.assertLessEqual(len(chunk), chunk_size)
      decompressed.append(chunk)
    self.assertEqual(original, b''.join(decompressed))

  def test_bad_zip_file(self):
    """Verify decompressing broken file raises IOError."""
    with self.assertRaises(IOError):
      b''.join(isolateserver.zip_decompress([b'Im not a zip file']))


class FakeItem(isolate_storage.Item):
  def __init__(self, data, high_priority=False):
    super(FakeItem, self).__init__(
      isolateserver_fake.hash_content(data), len(data), high_priority)
    self.data = data

  def content(self):
    return [self.data]

  @property
  def zipped(self):
    return zlib.compress(self.data, self.compression_level)


class MockedStorageApi(isolate_storage.StorageApi):
  # pylint: disable=abstract-method

  def __init__(
      self, server_ref, missing_hashes, push_side_effect=None):
    logging.debug(
        'MockedStorageApi.__init__(%s, %s)', server_ref, missing_hashes)
    # TODO(maruel): 'missing_hashes' is an anti-pattern.
    self._server_ref = server_ref
    self._missing_hashes = missing_hashes
    self._push_side_effect = push_side_effect
    self.push_calls = []
    self.contains_calls = []

  @property
  def server_ref(self):
    return self._server_ref

  def push(self, item, push_state, content=None):
    logging.debug(
        'MockedStorageApi.push(%s, %s, %s)', item, push_state, content)
    content = b''.join(item.content() if content is None else content)
    self.push_calls.append((item, push_state, content))
    if self._push_side_effect:
      self._push_side_effect()

  def contains(self, items):
    self.contains_calls.append(items)
    missing = {}
    for item in items:
      if item.digest in self._missing_hashes:
        missing[item] = self._missing_hashes[item.digest]
    logging.debug('MockedStorageApi.contains(%s): %s', items, missing)
    return missing


class UtilsTest(TestCase):
  """Tests for helper methods in isolateserver file."""

  def assertFile(self, path, contents):
    self.assertTrue(fs.exists(path), 'File %s doesn\'t exist!' % path)
    with fs.open(path, 'r') as f:
      self.assertMultiLineEqual(contents, f.read())

  def test_file_read(self):
    # TODO(maruel): Write test for file_read generator (or remove it).
    pass

  @unittest.skipIf(sys.platform == 'win32', 'crbug.com/1148174')
  def test_fileobj_path(self):
    # No path on in-memory objects
    self.assertIs(None, isolateserver.fileobj_path(io.BytesIO(b'hello')))

    # Path on opened files
    thisfile = os.path.join(test_env.TESTS_DIR, 'isolateserver_test.py')
    f = fs.open(thisfile)
    result = isolateserver.fileobj_path(f)
    self.assertIsInstance(result, six.text_type)
    self.assertSequenceEqual(result, thisfile)

    # Path on temporary files
    tf = tempfile.NamedTemporaryFile()
    result = isolateserver.fileobj_path(tf)
    self.assertIsInstance(result, six.text_type)
    self.assertSequenceEqual(result, tf.name)

    # No path on files which are no longer on the file system
    tf = tempfile.NamedTemporaryFile(delete=False)
    name = tf.name
    if six.PY2:
      name = name.decode(sys.getfilesystemencoding())
    fs.unlink(name)
    self.assertIs(None, isolateserver.fileobj_path(tf))

  def test_fileobj_copy_simple(self):
    inobj = io.BytesIO(b'hello')
    outobj = io.BytesIO()

    isolateserver.fileobj_copy(outobj, inobj)
    self.assertEqual(b'hello', outobj.getvalue())

  def test_fileobj_copy_partial(self):
    inobj = io.BytesIO(b'adatab')
    outobj = io.BytesIO()
    inobj.read(1)

    isolateserver.fileobj_copy(outobj, inobj, size=4)
    self.assertEqual(b'data', outobj.getvalue())

  def test_fileobj_copy_partial_file_no_size(self):
    with self.assertRaises(IOError):
      inobj = io.BytesIO(b'hello')
      outobj = io.BytesIO()

      inobj.read(1)
      isolateserver.fileobj_copy(outobj, inobj)

  def test_fileobj_copy_size_but_file_short(self):
    with self.assertRaises(IOError):
      inobj = io.BytesIO(b'hello')
      outobj = io.BytesIO()

      isolateserver.fileobj_copy(outobj, inobj, size=10)

  @unittest.skipIf(sys.platform == 'win32' and six.PY3, 'crbug.com/1182016')
  def test_putfile(self):
    tmpoutdir = None
    tmpindir = None

    try:
      tmpindir = tempfile.mkdtemp(prefix='isolateserver_test')
      infile = os.path.join(tmpindir, u'in')
      with fs.open(infile, 'wb') as f:
        f.write(b'data')

      tmpoutdir = tempfile.mkdtemp(prefix='isolateserver_test')

      # Copy as fileobj
      fo = os.path.join(tmpoutdir, u'fo')
      isolateserver.putfile(io.BytesIO(b'data'), fo)
      self.assertEqual(True, fs.exists(fo))
      self.assertEqual(False, fs.islink(fo))
      self.assertFile(fo, 'data')

      # Copy with partial fileobj
      pfo = os.path.join(tmpoutdir, u'pfo')
      fobj = io.BytesIO(b'adatab')
      fobj.read(1)  # Read the 'a'
      isolateserver.putfile(fobj, pfo, size=4)
      self.assertEqual(True, fs.exists(pfo))
      self.assertEqual(False, fs.islink(pfo))
      self.assertEqual(b'b', fobj.read())
      self.assertFile(pfo, 'data')

      # Copy as not readonly
      cp = os.path.join(tmpoutdir, u'cp')
      with fs.open(infile, 'rb') as f:
        isolateserver.putfile(f, cp, file_mode=0o755)
      self.assertEqual(True, fs.exists(cp))
      self.assertEqual(False, fs.islink(cp))
      self.assertFile(cp, 'data')

      # Use hardlink
      hl = os.path.join(tmpoutdir, u'hl')
      with fs.open(infile, 'rb') as f:
        isolateserver.putfile(f, hl, use_symlink=False)
      self.assertEqual(True, fs.exists(hl))
      self.assertEqual(False, fs.islink(hl))
      self.assertFile(hl, 'data')

      # Use symlink
      sl = os.path.join(tmpoutdir, u'sl')
      with fs.open(infile, 'rb') as f:
        isolateserver.putfile(f, sl, use_symlink=True)
      self.assertEqual(True, fs.exists(sl))
      self.assertEqual(True, fs.islink(sl))
      with fs.open(sl, 'rb') as f:
        self.assertEqual(b'data', f.read())
      self.assertFile(sl, 'data')

    finally:
      if tmpindir:
        file_path.rmtree(tmpindir)
      if tmpoutdir:
        file_path.rmtree(tmpoutdir)

  def test_fetch_stream_verifier_success(self):
    def teststream():
      yield b'abc'
      yield b'123'

    d = hashlib.sha1(b'abc123').hexdigest()
    verifier = isolateserver.FetchStreamVerifier(teststream(),
                                                 hashlib.sha1, d, 6)
    for _ in verifier.run():
      pass

  def test_fetch_stream_verifier_bad_size(self):
    def teststream():
      yield b'abc'
      yield b'123'

    d = hashlib.sha1(b'abc123').hexdigest()
    verifier = isolateserver.FetchStreamVerifier(teststream(),
                                                 hashlib.sha1, d, 7)
    failed = False
    try:
      for _ in verifier.run():
        pass
    except IOError:
      failed = True
    self.assertEqual(True, failed)

  def test_fetch_stream_verifier_bad_digest(self):
    def teststream():
      yield b'abc'
      yield b'123'

    d = hashlib.sha1(b'def456').hexdigest()
    verifier = isolateserver.FetchStreamVerifier(teststream(),
                                                 hashlib.sha1, d, 6)
    failed = False
    try:
      for _ in verifier.run():
        pass
    except IOError:
      failed = True
    self.assertEqual(True, failed)


class StorageTest(TestCase):
  """Tests for Storage methods."""

  def assertEqualIgnoringOrder(self, a, b):
    """Asserts that containers |a| and |b| contain same items."""
    self.assertEqual(len(a), len(b))
    self.assertEqual(set(a), set(b))

  def get_push_state(self, storage, item):
    missing = list(storage._storage_api.contains([item]).items())
    self.assertEqual(1, len(missing))
    self.assertEqual(item, missing[0][0])
    return missing[0][1]

  def test_upload_items(self):
    server_ref = isolate_storage.ServerRef('http://localhost:1', 'default')
    items = [
        isolateserver.BufferItem(b'a' * 12, server_ref.hash_algo),
        isolateserver.BufferItem(b'', server_ref.hash_algo),
        isolateserver.BufferItem(b'c' * 1222, server_ref.hash_algo),
        isolateserver.BufferItem(b'd' * 1223, server_ref.hash_algo),
    ]
    missing = {
      items[2]: 123,
      items[3]: 456,
    }
    storage_api = MockedStorageApi(
        server_ref,
        {item.digest: push_state for item, push_state in missing.items()})
    storage = isolateserver.Storage(storage_api)

    # Intentionally pass a generator, to confirm it works.
    result = storage.upload_items((i for i in items))
    six.assertCountEqual(self, missing, result)
    self.assertEqual(4, len(items))
    self.assertEqual(2, len(missing))
    self.assertEqual([items], storage_api.contains_calls)
    six.assertCountEqual(self, ((items[2], 123, items[2].content()[0]),
                                (items[3], 456, items[3].content()[0])),
                         storage_api.push_calls)

  def test_upload_items_empty(self):
    server_ref = isolate_storage.ServerRef('http://localhost:1', 'default')
    storage_api = MockedStorageApi(server_ref, {})
    storage = isolateserver.Storage(storage_api)
    result = storage.upload_items(())
    self.assertEqual([], result)

  def test_async_push(self):
    for use_zip in (False, True):
      item = FakeItem(b'1234567')
      server_ref = isolate_storage.ServerRef(
          'http://localhost:1', 'default-gzip' if use_zip else 'default')
      storage_api = MockedStorageApi(server_ref, {item.digest: 'push_state'})
      storage = isolateserver.Storage(storage_api)
      channel = threading_utils.TaskChannel()
      storage._async_push(channel, item, self.get_push_state(storage, item))
      # Wait for push to finish.
      pushed_item = next(channel)
      self.assertEqual(item, pushed_item)
      # StorageApi.push was called with correct arguments.
      self.assertEqual(
          [(item, 'push_state', item.zipped if use_zip else item.data)],
          storage_api.push_calls)

  def test_async_push_generator_errors(self):
    class FakeException(Exception):
      pass

    def faulty_generator():
      yield b'Hi!'
      raise FakeException('fake exception')

    for use_zip in (False, True):
      item = FakeItem(b'')
      self.mock(item, 'content', faulty_generator)
      server_ref = isolate_storage.ServerRef(
          'http://localhost:1', 'default-gzip' if use_zip else 'default')
      storage_api = MockedStorageApi(server_ref, {item.digest: 'push_state'})
      storage = isolateserver.Storage(storage_api)
      channel = threading_utils.TaskChannel()
      storage._async_push(channel, item, self.get_push_state(storage, item))
      with self.assertRaises(FakeException):
        next(channel)
      # StorageApi's push should never complete when data can not be read.
      self.assertEqual(0, len(storage_api.push_calls))

  def test_async_push_upload_errors(self):
    chunk = b'data_chunk'

    def push_side_effect():
      raise IOError('Nope')

    content_sources = (
        lambda: [chunk],
        lambda: [(yield chunk)],
    )

    for use_zip in (False, True):
      for source in content_sources:
        item = FakeItem(chunk)
        self.mock(item, 'content', source)
        server_ref = isolate_storage.ServerRef(
            'http://localhost:1', 'default-gzip' if use_zip else 'default')
        storage_api = MockedStorageApi(
            server_ref, {item.digest: 'push_state'}, push_side_effect)
        storage = isolateserver.Storage(storage_api)
        channel = threading_utils.TaskChannel()
        storage._async_push(channel, item, self.get_push_state(storage, item))
        with self.assertRaises(IOError):
          next(channel)
        # First initial attempt + all retries.
        attempts = 1 + storage.net_thread_pool.RETRIES
        # Single push attempt call arguments.
        expected_push = (
            item, 'push_state', item.zipped if use_zip else item.data)
        # Ensure all pushes are attempted.
        self.assertEqual(
            [expected_push] * attempts, storage_api.push_calls)

  def test_archive_files_to_storage(self):
    # Mocked
    files_content = {}
    def add(p, c):
      with open(os.path.join(self.tempdir, p), 'wb') as f:
        f.write(c)
      files_content[p] = c

    add(u'a', b'a' * 100)
    add(u'b', b'b' * 200)
    os.mkdir(os.path.join(self.tempdir, 'sub'))
    add(os.path.join(u'sub', u'c'), b'c' * 300)
    add(os.path.join(u'sub', u'a_copy'), b'a' * 100)

    files_hash = {
      p: hashlib.sha1(c).hexdigest() for p, c in files_content.items()
    }
    # 'a' and 'sub/c' are missing.
    missing = {
      files_hash[u'a']: u'a',
      files_hash[os.path.join(u'sub', u'c')]: os.path.join(u'sub', u'c'),
    }
    server_ref = isolate_storage.ServerRef(
        'http://localhost:1', 'some-namespace')
    storage_api = MockedStorageApi(server_ref, missing)
    storage = isolateserver.Storage(storage_api)
    with storage:
      results, cold, hot = isolateserver.archive_files_to_storage(
          storage, [os.path.join(self.tempdir, p) for p in files_content], None)
    self.assertEqual(
        {os.path.join(self.tempdir, f): h for f, h in files_hash.items()},
        dict(results))

    expected = [
      (os.path.join(self.tempdir, u'a'), files_hash['a']),
      (os.path.join(self.tempdir, u'sub', u'c'),
        files_hash[os.path.join(u'sub', u'c')]),
      (os.path.join(self.tempdir, u'sub', u'a_copy'),
        files_hash[os.path.join(u'sub', u'a_copy')]),
    ]
    self.assertEqual(expected, [(f.path, f.digest) for f in cold])
    self.assertEqual(
        [(os.path.join(self.tempdir, u'b'), files_hash['b'])],
        [(f.path, f.digest) for f in hot])
    # 'contains' checked for existence of all files.
    self.assertEqualIgnoringOrder(
        set(files_hash.values()),
        [i.digest for i in sum(storage_api.contains_calls, [])])
    # Pushed only missing files.
    self.assertEqualIgnoringOrder(
        list(missing),
        [call[0].digest for call in storage_api.push_calls])
    # Pushing with correct data, size and push state.
    for pushed_item, _push_state, pushed_content in storage_api.push_calls:
      filename = missing[pushed_item.digest]
      self.assertEqual(os.path.join(self.tempdir, filename), pushed_item.path)
      self.assertEqual(files_content[filename], pushed_content)

  @unittest.skipIf(sys.platform == 'win32', 'crbug.com/1148174')
  def test_archive_files_to_storage_symlink(self):
    link_path = os.path.join(self.tempdir, u'link')
    with open(os.path.join(self.tempdir, u'foo'), 'wb') as f:
      f.write(b'fooo')
    fs.symlink('foo', link_path)
    server_ref = isolate_storage.ServerRef('http://localhost:1', 'default')
    storage_api = MockedStorageApi(server_ref, {})
    storage = isolateserver.Storage(storage_api)
    results, cold, hot = isolateserver.archive_files_to_storage(
        storage, [self.tempdir], None)
    self.assertEqual([self.tempdir], list(results.keys()))
    self.assertEqual([], cold)
    # isolated, symlink, foo file.
    self.assertEqual(3, len(hot))
    self.assertEqual(os.path.join(self.tempdir, u'foo'), hot[0].path)
    self.assertEqual(4, hot[0].size)
    # TODO(maruel): The symlink is reported as its destination. We should fix
    # this because it double counts the stats.
    self.assertEqual(os.path.join(self.tempdir, u'foo'), hot[1].path)
    self.assertEqual(4, hot[1].size)
    # The isolated file is pure in-memory.
    self.assertIsInstance(hot[2], isolateserver.BufferItem)

  def test_archive_files_to_storage_tar(self):
    # Create 5 files, which is the minimum to create a tarball.
    for i in range(5):
      with open(os.path.join(self.tempdir, six.text_type(i)), 'wb') as f:
        f.write(b'fooo%d' % i)
    server_ref = isolate_storage.ServerRef('http://localhost:1', 'default')
    storage_api = MockedStorageApi(server_ref, {})
    storage = isolateserver.Storage(storage_api)
    results, cold, hot = isolateserver.archive_files_to_storage(
        storage, [self.tempdir], None)
    self.assertEqual([self.tempdir], list(results.keys()))
    self.assertEqual([], cold)
    # 5 files, the isolated file.
    self.assertEqual(6, len(hot))


class IsolateServerStorageApiTest(TestCase):
  @staticmethod
  def mock_fetch_request(server_ref, item, data=None, offset=0):
    compression = 'flate' if server_ref.is_with_compression else ''
    if data is None:
      response = {
        'url': '%s/some/gs/url/%s/%s' % (
            server_ref.url, server_ref.namespace, item),
      }
    else:
      response = {'content': base64.b64encode(data[offset:])}
    return (
      '%s/_ah/api/isolateservice/v1/retrieve' % server_ref.url,
      {
          'data': {
              'digest': item,
              'namespace': {
                  'compression': compression,
                  'digest_hash': 'sha-1',
                  'namespace': server_ref.namespace,
              },
              'offset': offset,
          },
          'read_timeout': 60,
      },
      response,
    )

  @staticmethod
  def mock_server_details_request(server_ref):
    return (
        '%s/_ah/api/isolateservice/v1/server_details' % server_ref.url,
        {'data': {}},
        {'server_version': 'such a good version'}
    )

  def mock_gs_request(self, server_ref, item, data=None, offset=0,
                      request_headers=None, response_headers=None):
    self.assertEqual(200, offset)
    self.assertEqual({'Range': 'bytes=200-'}, request_headers)
    response = data
    return (
        '%s/some/gs/url/%s/%s' % (server_ref.url, server_ref.namespace, item),
        {},
        response,
        response_headers,
    )

  @staticmethod
  def mock_contains_request(server_ref, request, response):
    url = server_ref.url + '/_ah/api/isolateservice/v1/preupload'
    digest_collection = dict(request, namespace={
        'compression': 'flate' if server_ref.is_with_compression else '',
        'digest_hash': server_ref.hash_algo_name,
        'namespace': server_ref.namespace,
    })
    return (url, {'data': digest_collection}, response)

  @staticmethod
  def mock_upload_request(server_ref, content, ticket, response=None):
    url = server_ref.url + '/_ah/api/isolateservice/v1/store_inline'
    request = {'content': content, 'upload_ticket': ticket}
    return (url, {'data': request}, response)

  def test_server_capabilities_success(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    self.expected_requests([self.mock_server_details_request(server_ref)])
    storage = isolate_storage.IsolateServer(server_ref)
    caps = storage._server_capabilities
    self.assertEqual({'server_version': 'such a good version'}, caps)

  def test_fetch_success(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    data = b''.join(str(x).encode() for x in range(1000))
    item = isolateserver_fake.hash_content(data)
    self.expected_requests([self.mock_fetch_request(server_ref, item, data)])
    storage = isolate_storage.IsolateServer(server_ref)
    fetched = b''.join(storage.fetch(item, 0, 0))
    self.assertEqual(data, fetched)

  def test_fetch_failure(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    item = isolateserver_fake.hash_content(b'something')
    self.expected_requests(
        [self.mock_fetch_request(server_ref, item)[:-1] + (None,)])
    storage = isolate_storage.IsolateServer(server_ref)
    with self.assertRaises(IOError):
      _ = ''.join(storage.fetch(item, 0, 0))

  def test_fetch_offset_success(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    data = b''.join(str(x).encode() for x in range(1000))
    item = isolateserver_fake.hash_content(data)
    offset = 200
    size = len(data)

    good_content_range_headers = [
      'bytes %d-%d/%d' % (offset, size - 1, size),
      'bytes %d-%d/*' % (offset, size - 1),
    ]

    for _content_range_header in good_content_range_headers:
      self.expected_requests(
          [self.mock_fetch_request(server_ref, item, data, offset=offset)])
      storage = isolate_storage.IsolateServer(server_ref)
      fetched = b''.join(storage.fetch(item, 0, offset))
      self.assertEqual(data[offset:], fetched)

  def test_fetch_offset_bad_header(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    data = b''.join(str(x).encode() for x in range(1000))
    item = isolateserver_fake.hash_content(data)
    offset = 200
    size = len(data)

    bad_content_range_headers = [
      # Missing header.
      None,
      '',
      # Bad format.
      'not bytes %d-%d/%d' % (offset, size - 1, size),
      'bytes %d-%d' % (offset, size - 1),
      # Bad offset.
      'bytes %d-%d/%d' % (offset - 1, size - 1, size),
      # Incomplete chunk.
      'bytes %d-%d/%d' % (offset, offset + 10, size),
    ]

    for content_range_header in bad_content_range_headers:
      self.expected_requests([
          self.mock_fetch_request(server_ref, item, offset=offset),
          self.mock_gs_request(
              server_ref, item, data, offset=offset,
              request_headers={'Range': 'bytes=%d-' % offset},
              response_headers={'Content-Range': content_range_header}),
      ])
      storage = isolate_storage.IsolateServer(server_ref)
      with self.assertRaises(IOError):
        _ = ''.join(storage.fetch(item, 0, offset))

  def test_push_success(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    data = b''.join(str(x).encode() for x in range(1000))
    item = FakeItem(data)
    contains_request = {'items': [
        {'digest': item.digest, 'size': item.size, 'is_isolated': 0}]}
    contains_response = {'items': [{'index': 0, 'upload_ticket': 'ticket!'}]}
    requests = [
        self.mock_contains_request(server_ref, contains_request,
                                   contains_response),
        self.mock_upload_request(
            server_ref,
            base64.b64encode(data).decode(),
            contains_response['items'][0]['upload_ticket'],
            {'ok': True},
        ),
    ]
    self.expected_requests(requests)
    storage = isolate_storage.IsolateServer(server_ref)
    missing = storage.contains([item])
    self.assertEqual([item], list(missing.keys()))
    push_state = missing[item]
    storage.push(item, push_state, [data])
    self.assertTrue(push_state.uploaded)
    self.assertTrue(push_state.finalized)

  def test_push_failure_upload(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    data = b''.join(str(x).encode() for x in range(1000))
    item = FakeItem(data)
    contains_request = {'items': [
        {'digest': item.digest, 'size': item.size, 'is_isolated': 0}]}
    contains_response = {'items': [{'index': 0, 'upload_ticket': 'ticket!'}]}
    requests = [
        self.mock_contains_request(server_ref, contains_request,
                                   contains_response),
        self.mock_upload_request(
            server_ref,
            base64.b64encode(data).decode(),
            contains_response['items'][0]['upload_ticket'],
        ),
    ]
    self.expected_requests(requests)
    storage = isolate_storage.IsolateServer(server_ref)
    missing = storage.contains([item])
    self.assertEqual([item], list(missing.keys()))
    push_state = missing[item]
    with self.assertRaises(IOError):
      storage.push(item, push_state, [data])
    self.assertFalse(push_state.uploaded)
    self.assertFalse(push_state.finalized)

  def test_push_failure_finalize(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    data = b''.join(str(x).encode() for x in range(1000))
    item = FakeItem(data)
    contains_request = {'items': [
        {'digest': item.digest, 'size': item.size, 'is_isolated': 0}]}
    contains_response = {'items': [
        {'index': 0,
         'gs_upload_url': '%s/FAKE_GCS/whatevs/1234' % server_ref.url,
         'upload_ticket': 'ticket!'}]}
    requests = [
        self.mock_contains_request(server_ref, contains_request,
                                   contains_response),
        (
            '%s/FAKE_GCS/whatevs/1234' % server_ref.url,
            {
                'data': data,
                'content_type': 'application/octet-stream',
                'method': 'PUT',
                'headers': {
                    'Cache-Control': 'public, max-age=31536000'
                },
            },
            b'',
            {
                'x-goog-hash':
                    'md5=' +
                    base64.b64encode(hashlib.md5(data).digest()).decode(),
            },
        ),
        (
            '%s/_ah/api/isolateservice/v1/finalize_gs_upload' % server_ref.url,
            {
                'data': {
                    'upload_ticket': 'ticket!'
                }
            },
            None,
        ),
    ]
    self.expected_requests(requests)
    storage = isolate_storage.IsolateServer(server_ref)
    missing = storage.contains([item])
    self.assertEqual([item], list(missing.keys()))
    push_state = missing[item]
    with self.assertRaises(IOError):
      storage.push(item, push_state, [data])
    self.assertTrue(push_state.uploaded)
    self.assertFalse(push_state.finalized)

  def test_contains_success(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    files = [
        FakeItem(b'1', high_priority=True),
        FakeItem(b'2' * 100),
        FakeItem(b'3' * 200),
    ]
    request = {'items': [
        {'digest': f.digest, 'is_isolated': not i, 'size': f.size}
        for i, f in enumerate(files)]}
    response = {
        'items': [{
            'index': str(i),
            'upload_ticket': 'ticket_%d' % i
        } for i in range(3)],
    }
    missing = [
        files[0],
        files[1],
        files[2],
    ]
    self._requests = [self.mock_contains_request(server_ref, request, response)]
    storage = isolate_storage.IsolateServer(server_ref)
    result = storage.contains(files)
    self.assertEqual(set(missing), set(result.keys()))
    for i, (_item, push_state) in enumerate(result.items()):
      self.assertEqual(
          push_state.upload_url, '_ah/api/isolateservice/v1/store_inline')
      self.assertEqual(push_state.finalize_url, None)

  def test_contains_network_failure(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    self.expected_requests(
        [self.mock_contains_request(server_ref, {'items': []}, None)])
    storage = isolate_storage.IsolateServer(server_ref)
    with self.assertRaises(isolated_format.MappingError):
      storage.contains([])

  def test_contains_format_failure(self):
    server_ref = isolate_storage.ServerRef('http://example.com', 'default')
    self.expected_requests(
        [self.mock_contains_request(server_ref, {'items': []}, None)])
    storage = isolate_storage.IsolateServer(server_ref)
    with self.assertRaises(isolated_format.MappingError):
      storage.contains([])


@parameterized.parameterized_class(('verify_push',), [(True,), (False,)])
class IsolateServerStorageSmokeTest(unittest.TestCase):
  """Tests public API of Storage class using file system as a store."""
  # These tests fail when running with other tests
  # Need to run in test_seq.py
  no_run = 1

  def setUp(self):
    super(IsolateServerStorageSmokeTest, self).setUp()
    self.tempdir = tempfile.mkdtemp(prefix=u'isolateserver')
    self.server = isolateserver_fake.FakeIsolateServer()

  def tearDown(self):
    try:
      file_path.rmtree(self.tempdir)
      self.server.close()
    finally:
      super(IsolateServerStorageSmokeTest, self).tearDown()

  def run_upload_items_test(self, namespace):
    storage = isolateserver.get_storage(
        isolate_storage.ServerRef(self.server.url, namespace))

    # Items to upload.
    items = [
        isolateserver.BufferItem(b'item %d' % i, storage.server_ref.hash_algo)
        for i in range(10)
    ]

    # Do it.
    uploaded = storage.upload_items(items, self.verify_push)
    self.assertEqual(set(items), set(uploaded))

    # Now ensure upload_items skips existing items.
    more = [
        isolateserver.BufferItem(b'more item %d' % i,
                                 storage.server_ref.hash_algo)
        for i in range(10)
    ]

    # Uploaded only |more|.
    uploaded = storage.upload_items(items + more)
    self.assertEqual(set(more), set(uploaded))

  def test_upload_items(self):
    self.run_upload_items_test('default')

  def test_upload_items_verify_failed(self):
    storage = isolateserver.get_storage(
        isolate_storage.ServerRef(self.server.url, 'default'))

    # Items to upload.
    items = [
        isolateserver.BufferItem(b'item ' * 200, storage.server_ref.hash_algo)
    ]

    called = []

    orig_fetch = storage._fetch

    def mocked_fetch(digest, size, sink):
      called.append(True)
      if len(called) != 1:
        orig_fetch(digest, size, sink)
        return
      # raise error in first call to cause retry.
      raise IOError('exception from mock')

    with mock.patch.object(storage, '_fetch', mocked_fetch):
      storage.upload_items(items, verify_push=True)

  def test_upload_items_gzip(self):
    self.run_upload_items_test('default-gzip')

  def run_push_and_fetch_test(self, namespace):
    storage = isolateserver.get_storage(
        isolate_storage.ServerRef(self.server.url, namespace))

    # Upload items.
    items = [
        isolateserver.BufferItem(b'item %d' % i, storage.server_ref.hash_algo)
        for i in range(10)
    ]
    uploaded = storage.upload_items(items, self.verify_push)
    self.assertEqual(set(items), set(uploaded))

    # Fetch them all back into local memory cache.
    cache = local_caching.MemoryContentAddressedCache()
    queue = isolateserver.FetchQueue(storage, cache)

    # Start fetching.
    pending = set()
    for item in items:
      pending.add(item.digest)
      queue.add(item.digest)
      queue.wait_on(item.digest)

    # Wait for fetch to complete.
    while pending:
      fetched = queue.wait()
      pending.discard(fetched)

    # Ensure fetched same data as was pushed.
    actual = []
    for i in items:
      with cache.getfileobj(i.digest) as f:
        actual.append(f.read())

    self.assertEqual([b''.join(i.content()) for i in items], actual)

  def test_push_and_fetch(self):
    self.run_push_and_fetch_test('default')

  def test_push_and_fetch_gzip(self):
    self.run_push_and_fetch_test('default-gzip')

  def _archive_smoke(self, size):
    self.server.store_hash_instead()
    files = {}
    for i in range(5):
      name = '512mb_%d.%s' % (i, isolateserver.ALREADY_COMPRESSED_TYPES[0])
      logging.info('Writing %s', name)
      p = os.path.join(self.tempdir, name)
      h = hashlib.sha1()
      data = os.urandom(1024)
      with open(p, 'wb') as f:
        # Write 512MiB.
        for _ in range(size // len(data)):
          f.write(data)
          h.update(data)
      os.chmod(p, 0o600)
      files[p] = h.hexdigest()

    server_ref = isolate_storage.ServerRef(self.server.url, 'default')
    with isolateserver.get_storage(server_ref) as storage:
      logging.info('Archiving')
      results, cold, hot = isolateserver.archive_files_to_storage(
          storage, list(files), None)
      logging.info('Done')

    expected = {'default': {h: h for h in files.values()}}
    self.assertEqual(expected, self.server.contents)
    self.assertEqual(files, dict(results))
    # Everything is cold.
    f = os.path.join(self.tempdir, '512mb_3.7z')
    self.assertEqual(
        sorted(files.items()), sorted((f.path, f.digest) for f in cold))
    self.assertEqual([], [(f.path, f.digest) for f in hot])

  def test_archive_multiple_files(self):
    self._archive_smoke(512*1024)

  @unittest.skipIf(sys.maxsize > (2**31), 'Only running on 32 bits')
  def test_archive_multiple_huge_files(self):
    # Create multiple files over 2.5GiB. This test exists to stress the virtual
    # address space on 32 bits systems. Make real files since it wouldn't fit
    # memory by definition.
    # Sadly, this makes this test very slow so it's only run on 32-bit
    # platforms, since it's known to work on 64-bit platforms anyway.
    #
    # It's a fairly slow test, well over 15 seconds.
    self._archive_smoke(512*1024*1024)


class IsolateServerDownloadTest(TestCase):
  def _url_read_json(self, url, **kwargs):
    """Current _url_read_json mock doesn't respect identical URLs."""
    logging.warning('url_read_json(%s, %s)', url[:500], str(kwargs)[:500])
    with self._lock:
      if not self._requests:
        return None
      if not self._flagged_requests:
        self._flagged_requests = [0 for _element in self._requests]
      # Ignore 'stream' argument, it's not important for these tests.
      kwargs.pop('stream', None)
      for i, (new_url, expected_kwargs, result) in enumerate(self._requests):
        if new_url == url and expected_kwargs == kwargs:
          self._flagged_requests[i] = 1
          return result
    self.fail('Unknown request %s' % url)
    return None

  def _get_actual(self):
    """Returns the files in '<self.tempdir>/target'."""
    actual = {}
    for root, _dirs, files in os.walk(os.path.join(self.tempdir, 'target')):
      for item in files:
        p = os.path.join(root, item)
        with open(p, 'rb') as f:
          content = f.read()
        if os.path.islink(p):
          actual[p] = (os.readlink(p), 0)
        else:
          actual[p] = (content, os.stat(p).st_mode & 0o777)
    return actual

  def setUp(self):
    super(IsolateServerDownloadTest, self).setUp()
    self._flagged_requests = []
    self.mock(logging_utils, 'prepare_logging', lambda *_: None)
    self.mock(logging_utils, 'set_console_level', lambda *_: None)

  def tearDown(self):
    if all(self._flagged_requests):
      self._requests = []
    super(IsolateServerDownloadTest, self).tearDown()

  def test_download_two_files(self):
    # Test downloading two files.
    # It doesn't touch disk, 'file_write' is mocked.
    # It doesn't touch network, url_open() is mocked.
    actual = {}
    def out(key, generator):
      actual[key] = b''.join(generator)

    self.mock(local_caching, 'file_write', out)
    server_ref = isolate_storage.ServerRef('http://example.com', 'default-gzip')
    coucou_sha1 = hashlib.sha1(b'Coucou').hexdigest()
    byebye_sha1 = hashlib.sha1(b'Bye Bye').hexdigest()
    requests = [(
        '%s/_ah/api/isolateservice/v1/retrieve' % server_ref.url,
        {
            'data': {
                'digest': six.ensure_str(h),
                'namespace': {
                    'namespace': 'default-gzip',
                    'digest_hash': 'sha-1',
                    'compression': 'flate',
                },
                'offset': 0,
            },
            'read_timeout': 60,
        },
        {
            'content': base64.b64encode(zlib.compress(v)).decode()
        },
    ) for h, v in [(coucou_sha1, b'Coucou'), (byebye_sha1, b'Bye Bye')]]
    self.expected_requests(requests)
    cmd = [
      'download',
      '--isolate-server', server_ref.url,
      '--namespace', server_ref.namespace,
      '--target', test_env.CLIENT_DIR,
      '--file', coucou_sha1, 'path/to/a',
      '--file', byebye_sha1, 'path/to/b',
      # Even if everything is mocked, the cache directory will still be created.
      '--cache', self.tempdir,
    ]
    self.assertEqual(0, isolateserver.main(cmd))
    expected = {
        os.path.join(test_env.CLIENT_DIR, 'path/to/a'): b'Coucou',
        os.path.join(test_env.CLIENT_DIR, 'path/to/b'): b'Bye Bye',
    }
    self.assertEqual(expected, actual)

  @unittest.skipIf(sys.platform == 'win32', 'crbug.com/1148174')
  def test_download_isolated_simple(self):
    # Test downloading an isolated tree.
    # It writes files to disk for real.
    server_ref = isolate_storage.ServerRef('http://example.com', 'default-gzip')
    files = {
        os.path.join('a', 'foo'): b'Content',
        'b': b'More content',
    }
    isolated = {
        'command': ['Absurb', 'command'],
        'relative_cwd': 'a',
        'files': {
            os.path.join('a', 'foo'): {
                'h': isolateserver_fake.hash_content(b'Content'),
                's': len('Content'),
                'm': 0o700,
            },
            'b': {
                'h': isolateserver_fake.hash_content(b'More content'),
                's': len('More content'),
                'm': 0o600,
            },
            'c': {
                'l': 'a/foo',
            },
        },
        'version': isolated_format.ISOLATED_FILE_VERSION,
    }
    isolated_data = json.dumps(
        isolated, sort_keys=True, separators=(',', ':')).encode()
    isolated_hash = isolateserver_fake.hash_content(isolated_data)
    requests = [
      (v['h'], files[k]) for k, v in isolated['files'].items()
      if 'h' in v
    ]
    requests.append((isolated_hash, isolated_data))
    requests = [(
        '%s/_ah/api/isolateservice/v1/retrieve' % server_ref.url,
        {
            'data': {
                'digest': six.ensure_str(h),
                'namespace': {
                    'namespace': 'default-gzip',
                    'digest_hash': 'sha-1',
                    'compression': 'flate',
                },
                'offset': 0,
            },
            'read_timeout': 60,
        },
        {
            'content': base64.b64encode(zlib.compress(v)).decode()
        },
    ) for h, v in requests]
    cmd = [
      'download',
      '--isolate-server', server_ref.url,
      '--namespace', server_ref.namespace,
      '--target', os.path.join(self.tempdir, 'target'),
      '--isolated', isolated_hash,
      '--cache', os.path.join(self.tempdir, 'cache'),
    ]
    self.expected_requests(requests)
    self.assertEqual(0, isolateserver.main(cmd))
    expected = {
        os.path.join(self.tempdir, 'target', 'a', 'foo'): (b'Content', 0o700),
        os.path.join(self.tempdir, 'target', 'b'): (b'More content', 0o600),
        os.path.join(self.tempdir, 'target', 'c'): ('a/foo', 0),
    }
    actual = self._get_actual()
    self.assertEqual(expected, actual)
    expected_stdout = [
        mock.call(
            'To run this test please run from the directory %s:' %
            os.path.join(self.tempdir, 'target', 'a'),),
        mock.call('  Absurb command',),
    ]
    self.checkOutput(expected_stdout)

  @unittest.skipIf(sys.platform == 'win32', 'crbug.com/1148174')
  def test_download_isolated_tar_archive(self):
    # Test downloading an isolated tree.
    server_ref = isolate_storage.ServerRef('http://example.com', 'default-gzip')

    files = {
        os.path.join('a', 'foo'): (b'Content', 0o500),
        'b': (b'More content', 0o600),
        'c': (b'Even more content!', 0o500),
    }

    # Generate a tar archive
    tf = io.BytesIO()
    with tarfile.TarFile(mode='w', fileobj=tf) as tar:
      f1 = tarfile.TarInfo()
      f1.type = tarfile.REGTYPE
      f1.name = 'a/foo'
      f1.size = 7
      f1.mode = 0o570
      tar.addfile(f1, io.BytesIO(b'Content'))

      f2 = tarfile.TarInfo()
      f2.type = tarfile.REGTYPE
      f2.name = 'b'
      f2.size = 12
      f2.mode = 0o666
      tar.addfile(f2, io.BytesIO(b'More content'))
    archive = tf.getvalue()

    isolated = {
      'command': ['Absurb', 'command'],
      'relative_cwd': 'a',
      'files': {
        'archive1': {
          'h': isolateserver_fake.hash_content(archive),
          's': len(archive),
          't': 'tar',
        },
        'c': {
          'h': isolateserver_fake.hash_content(files['c'][0]),
          's': len(files['c'][0]),
        },
      },
      'version': isolated_format.ISOLATED_FILE_VERSION,
    }
    isolated_data = json.dumps(
        isolated, sort_keys=True, separators=(',', ':')).encode()
    isolated_hash = isolateserver_fake.hash_content(isolated_data)
    requests = [
      (isolated['files']['archive1']['h'], archive),
      (isolated['files']['c']['h'], files['c'][0]),
    ]
    requests.append((isolated_hash, isolated_data))
    requests = [(
        '%s/_ah/api/isolateservice/v1/retrieve' % server_ref.url,
        {
            'data': {
                'digest': six.ensure_str(h),
                'namespace': {
                    'namespace': 'default-gzip',
                    'digest_hash': 'sha-1',
                    'compression': 'flate',
                },
                'offset': 0,
            },
            'read_timeout': 60,
        },
        {
            'content': base64.b64encode(zlib.compress(v)).decode()
        },
    ) for h, v in requests]
    cmd = [
      'download',
      '--isolate-server', server_ref.url,
      '--namespace', server_ref.namespace,
      '--target', os.path.join(self.tempdir, 'target'),
      '--isolated', isolated_hash,
      '--cache', os.path.join(self.tempdir, 'cache'),
    ]
    self.expected_requests(requests)
    self.assertEqual(0, isolateserver.main(cmd))
    expected = {
      os.path.join(self.tempdir, 'target', k): v for k, v in files.items()
    }
    actual = self._get_actual()
    self.assertEqual(expected, actual)

    expected_stdout = [
        mock.call(u'To run this test please run from the directory %s:' %
                  os.path.join(self.tempdir, 'target', 'a')),
        mock.call(u'  Absurb command'),
    ]
    self.checkOutput(expected_stdout)


def get_storage(server_ref):
  class StorageFake(object):
    def __enter__(self, *_):
      return self

    def __exit__(self, *_):
      pass

    @property
    def server_ref(self):
      return server_ref

    @staticmethod
    def upload_items(items, _verify_push):
      # Always returns the last item as not present.
      return [list(items)[-1]]
  return StorageFake()


class TestArchive(TestCase):
  def setUp(self):
    super(TestArchive, self).setUp()
    self.mock(logging_utils, 'prepare_logging', lambda *_: None)
    self.mock(logging_utils, 'set_console_level', lambda *_: None)

  def test_archive_files(self):
    self.mock(isolateserver, 'get_storage', get_storage)
    self.make_tree(CONTENTS)
    f = ['empty_file.txt', 'small_file.txt']
    os.chdir(self.tempdir)
    isolateserver.main(
        ['archive', '--isolate-server', 'https://localhost:1'] + f)
    self.checkOutput([
        mock.call(u'da39a3ee5e6b4b0d3255bfef95601890afd80709 empty_file.txt\n'
                  '0491bd1da8087ad10fcdd7c9634e308804b72158 small_file.txt'),
    ])

  def help_test_archive(self, cmd_line_prefix):
    self.mock(isolateserver, 'get_storage', get_storage)
    self.make_tree(CONTENTS)
    isolateserver.main(cmd_line_prefix + [self.tempdir])
    isolated = {
      'algo': 'sha-1',
      'files': {},
      'version': isolated_format.ISOLATED_FILE_VERSION,
    }
    for k, v in CONTENTS.items():
      isolated['files'][k] = {
        'h': isolateserver_fake.hash_content(v),
        's': len(v),
      }
      if sys.platform != 'win32':
        isolated['files'][k]['m'] = 0o600
    isolated_data = json.dumps(
        isolated, sort_keys=True, separators=(',', ':')).encode()
    isolated_hash = isolateserver_fake.hash_content(isolated_data)
    self.checkOutput([mock.call('%s %s' % (isolated_hash, self.tempdir))])

  def test_archive_directory(self):
    self.help_test_archive(['archive', '-I', 'https://localhost:1'])

  def test_archive_directory_envvar(self):
    with test_env.EnvVars({'ISOLATE_SERVER': 'https://localhost:1'}):
      self.help_test_archive(['archive'])


if __name__ == '__main__':
  for e in ('ISOLATE_DEBUG', 'ISOLATE_SERVER'):
    os.environ.pop(e, None)
  test_env.main()
