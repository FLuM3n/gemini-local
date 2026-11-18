"""Offline integration checks: real loopback HTTP, fake Gemini upstream."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import http.client
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import gemini_api as api
import gemini_terminal as terminal


class FakeClient:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.maximum = 0
        self.started = threading.Event()
        self.cancelled = threading.Event()

    async def generate_content_stream(self, prompt, **kwargs):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        files = kwargs.get('files') or []
        self.calls.append((prompt, kwargs, [(p, p.read_bytes()) for p in files]))
        self.started.set()
        try:
            if 'hang-request' in prompt:
                await asyncio.sleep(30)
            if 'slow-request' in prompt:
                await asyncio.sleep(.08)
            if 'login-error' in prompt:
                raise terminal.LoginRequired('private-cookie-value')
            if 'early-error' in prompt:
                raise RuntimeError('failed private-cookie-value test-access-key')
            if 'empty-response' in prompt:
                return
            if 'fallback-response' in prompt:
                yield NS(text_delta='', text='Fallback')
                return
            yield NS(text_delta='你好', text='你好')
            if 'partial-error' in prompt:
                raise RuntimeError('upstream interrupted')
            yield NS(text_delta=' Gemini', text='你好 Gemini')
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1


class APITests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.worker = terminal.Worker()
        self.client = FakeClient()
        model = NS(model_name='gemini-flash', model_id='real-model-id', display_name='Flash', aliases=['flash'])
        self.session = NS(settings=terminal.Settings(Path(self.home.name), {'cookie': 'private-cookie-value'}),
                          model=model, models=[model], extended=False, client=self.client,
                          check_account=lambda: None, request_lock=asyncio.Lock())
        self.bridge = api.GeminiBridge(self.session, self.worker)
        self.server = api.LocalAPIServer(('127.0.0.1', 0), self.bridge)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.worker.close()
        self.home.cleanup()

    def request(self, body=None, path='/v1/chat/completions', headers=None, method=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        conn.request(method or ('POST' if body is not None else 'GET'), path, data, headers or {})
        response = conn.getresponse()
        status, content_type, raw = response.status, response.getheader('Content-Type'), response.read().decode()
        conn.close()
        return status, content_type, json.loads(raw) if 'application/json' in content_type else raw

    def body(self, text='hello', **kwargs):
        return {'model': 'gemini-flash', 'messages': [{'role': 'user', 'content': text}], **kwargs}

    def test_health_models_do_not_generate(self):
        self.assertEqual(self.request(path='/health')[2]['status'], 'ok')
        data = self.request(path='/v1/models')[2]['data']
        self.assertEqual([m['id'] for m in data], ['gemini-flash'])
        self.assertNotIn('context_window', data[0])
        self.assertEqual(self.client.calls, [])

    def test_full_history_no_truncation_and_isolated_calls(self):
        full_text = 'paper-full-text-' * 20000
        messages = [{'role': 'system', 'content': full_text}, {'role': 'user', 'content': 'old question'},
                    {'role': 'assistant', 'content': 'old answer'}, {'role': 'user', 'content': 'new question'}]
        status, _, response = self.request({'messages': messages})
        self.assertEqual(status, 200)
        self.assertEqual(response['choices'][0]['message']['content'], '你好 Gemini')
        prompt, kwargs, _ = self.client.calls[0]
        for text in (full_text, 'User: old question', 'Assistant: old answer', 'User: new question'):
            self.assertIn(text, prompt)
        self.assertNotIn('chat', kwargs)
        self.assertFalse(kwargs['extended_thinking'])
        self.assertTrue(kwargs['temporary'])
        self.request(self.body('different paper'))
        self.assertNotIn(full_text, self.client.calls[1][0])
        self.assertNotIn('old question', self.client.calls[1][0])

    def test_sse_compatible_chunks(self):
        status, kind, text = self.request(self.body(stream=True))
        self.assertEqual(status, 200)
        self.assertIn('text/event-stream', kind)
        lines = [line[6:] for line in text.splitlines() if line.startswith('data: ')]
        self.assertEqual(lines[-1], '[DONE]')
        chunks = [json.loads(line) for line in lines[:-1]]
        self.assertEqual(''.join(c['choices'][0]['delta'].get('content', '') for c in chunks), '你好 Gemini')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_images_correct_extension_and_cleanup(self):
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZooAAAAASUVORK5CYII=')
        content = [{'type': 'text', 'text': 'read this page'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}}]
        self.assertEqual(self.request(self.body(content))[0], 200)
        path, data = self.client.calls[0][2][0]
        self.assertEqual(path.suffix, '.png')
        self.assertEqual(data, image)
        self.assertFalse(path.exists())

    def test_strict_models_bad_payload_and_limits(self):
        self.assertEqual(self.request(self.body(model='made-up-model'))[0], 404)
        self.assertEqual(self.request(self.body(stream='yes'))[0], 400)
        self.assertEqual(self.request(self.body(tools=[{'type': 'function'}]))[0], 400)
        self.assertEqual(self.request({'messages': [{'role': 'tool', 'content': 'x'}]})[0], 400)
        self.assertEqual(self.request({'messages': []})[0], 400)
        self.assertEqual(self.request(self.body([{'type': 'image_url', 'image_url': {'url': 'https://example.com/x.png'}}]))[0], 400)
        with patch.object(api, 'MAX_REQUEST_BYTES', 2):
            self.assertEqual(self.request(self.body())[0], 413)
        with patch.object(api, 'MAX_IMAGE_BYTES', 1):
            self.assertEqual(self.request(self.body([{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,YWJj'}}]))[0], 413)
        self.assertEqual(self.client.calls, [])

    def test_errors_are_explicit_and_secrets_masked(self):
        self.bridge.api_key = 'test-access-key'
        headers = {'Authorization': 'Bearer test-access-key'}
        status, _, result = self.request(self.body('early-error', stream=True), headers=headers)
        self.assertEqual(status, 502)
        self.assertNotIn('private-cookie-value', result['error']['message'])
        self.assertNotIn('test-access-key', result['error']['message'])
        self.assertEqual(self.request(self.body('login-error'), headers=headers)[0], 401)
        self.assertEqual(self.request(self.body('empty-response'), headers=headers)[0], 502)
        self.assertEqual(self.request(self.body('fallback-response'), headers=headers)[2]['choices'][0]['message']['content'], 'Fallback')

    def test_partial_failure_notice_in_stream(self):
        status, _, text = self.request(self.body('partial-error', stream=True))
        self.assertEqual(status, 200)
        self.assertIn('本轮回答未完成', text)
        self.assertNotIn('[DONE]', text)

    def test_local_access_guards(self):
        self.assertEqual(self.request(path='/health', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request(path='/health', headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request(path='/health', headers={'Origin': 'chrome://zotero'})[0], 200)
        self.assertEqual(self.request(path='/health', headers={'Origin': 'null'})[0], 403)
        self.bridge.api_key = 'test-access-key'
        self.assertEqual(self.request(path='/health')[0], 401)
        self.assertEqual(self.request(path='/health', headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request(path='/health', headers={'Authorization': 'Bearer test-access-key', 'Origin': 'null'})[0], 200)

    def test_concurrent_requests_serialize_sdk(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.request(self.body('slow-request'))[0], range(3)))
        self.assertEqual(results, [200, 200, 200])
        self.assertEqual(self.client.maximum, 1)

    def test_terminal_and_api_use_same_lock(self):
        async def terminal_turn():
            async for _ in self.client.generate_content_stream('slow-request terminal'):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            turn = pool.submit(self.worker.run, terminal.NativeChat.serialized(self.session, terminal_turn))
            self.assertTrue(self.client.started.wait(2))
            self.assertEqual(self.request(self.body('slow-request Zotero'))[0], 200)
            turn.result(timeout=3)
        self.assertIs(self.bridge.lock, self.session.request_lock)
        self.assertEqual(self.client.maximum, 1)

    def test_api_thinking_does_not_follow_terminal_changes(self):
        self.session.extended = True
        self.assertEqual(self.request(self.body())[0], 200)
        self.assertFalse(self.client.calls[0][1]['extended_thinking'])
        self.assertEqual(self.request(self.body(extended_thinking=True))[0], 200)
        self.assertTrue(self.client.calls[1][1]['extended_thinking'])

    def test_background_lifecycle_stops_listener_and_active_request(self):
        with redirect_stdout(io.StringIO()):
            with api.background_service(self.session, self.worker, 0) as server:
                port = server.server_port
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                conn.request('GET', '/health')
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                future = server.bridge.submit('hang-request', [], self.session.model, False, api.queue.SimpleQueue())
                self.assertTrue(self.client.started.wait(2))
            self.assertTrue(self.client.cancelled.wait(2))
            self.assertTrue(future.cancelled())
            self.assertEqual(len(server.bridge.tasks), 0)
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', port), timeout=.2)
            with self.assertRaises(api.RequestError):
                server.bridge.submit('after shutdown', [], self.session.model, False, api.queue.SimpleQueue())

    def test_occupied_port_reports_actionable_error(self):
        with self.assertRaisesRegex(terminal.TerminalError, '端口 .* 已被占用'):
            api.create_server(self.session, self.worker, self.server.server_port)

    def test_disconnect_cancels_upstream_then_next_request_works(self):
        body = json.dumps(self.body('hang-request')).encode()
        conn = socket.create_connection(('127.0.0.1', self.server.server_port))
        conn.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: %d\r\n\r\n' % (self.server.server_port, len(body))).encode() + body)
        self.assertTrue(self.client.started.wait(2))
        conn.close()
        self.assertTrue(self.client.cancelled.wait(3))
        self.assertEqual(self.request(self.body())[0], 200)


class CLITests(unittest.TestCase):
    def run_main(self, args):
        calls = []
        class Session:
            def __init__(self, settings):
                self.settings = settings
                self.client = NS(usage_info={})
            async def connect(self, model):
                calls.append(('connect', model, self.extended, self.settings.cache_dir))
            async def close(self):
                calls.append(('close',))
        class Worker:
            def run(self, coroutine):
                return asyncio.run(coroutine)
            def close(self):
                pass
        @contextmanager
        def background(session, worker, port, *, extended):
            calls.append(('background', port, extended))
            try:
                yield
            finally:
                calls.append(('background closed',))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            def load(path, cache):
                return terminal.Settings(path, {}, cache_dir=cache)
            with patch.object(terminal.Settings, 'load', side_effect=load), patch.object(terminal, 'NativeChat', Session), patch.object(terminal, 'Worker', Worker), patch.object(terminal, 'interactive', side_effect=lambda *_: calls.append(('interactive',))), patch.object(terminal, 'print_models'), patch.object(terminal, 'print_selection'), patch.object(api, 'serve', side_effect=lambda session, worker, port: calls.append(('serve', port))), patch.object(api, 'background_service', side_effect=background), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = terminal.main(['--home', str(home), *args])
        return result, calls

    def test_default_still_terminal_extended(self):
        result, calls = self.run_main([])
        self.assertEqual(result, 0)
        self.assertIn(('interactive',), calls)
        self.assertEqual(calls[0], ('connect', None, True, None))
        self.assertIn(('background', 8100, False), calls)
        self.assertLess(calls.index(('background closed',)), calls.index(('close',)))

    def test_terminal_only_does_not_open_server(self):
        result, calls = self.run_main(['--terminal-only'])
        self.assertEqual(result, 0)
        self.assertIn(('interactive',), calls)
        self.assertFalse(any(row[0] == 'background' for row in calls))

    def test_combined_api_thinking_and_port(self):
        result, calls = self.run_main(['--api-thinking', 'extended', '--port', '8101'])
        self.assertEqual(result, 0)
        self.assertIn(('background', 8101, True), calls)

    def test_api_separate_cache_and_standard_launcher(self):
        result, calls = self.run_main(['--serve', '--thinking', 'standard', '--port', '8101'])
        self.assertEqual(result, 0)
        self.assertIn(('serve', 8101), calls)
        self.assertFalse(calls[0][2])
        self.assertEqual(calls[0][3].name, '.gemini-api-cache')
        self.assertNotIn(('interactive',), calls)

    def test_check_is_still_read_only(self):
        result, calls = self.run_main(['--check'])
        self.assertEqual(result, 0)
        self.assertNotIn(('interactive',), calls)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
