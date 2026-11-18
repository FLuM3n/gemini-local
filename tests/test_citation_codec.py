import asyncio
import queue
from types import SimpleNamespace as NS
import unittest

from gemini_api import CitationCodec, GeminiBridge
from gemini_terminal import Settings
from pathlib import Path


class CodecTests(unittest.TestCase):
    def test_roundtrip_at_every_chunk_boundary(self):
        prompt = 'Document [[cite:eabc001, eabc002]] evidence.'
        output = 'First [AIDEA_SOURCE:eabc001] and [AIDEA_SOURCE:eabc002].'
        expected = 'First [[cite:eabc001]] and [[cite:eabc002]].'
        for index in range(len(output) + 1):
            codec = CitationCodec(prompt)
            self.assertNotIn('[[cite:', codec.prompt)
            self.assertEqual(codec.feed(output[:index]) + codec.feed(output[index:]) + codec.feed('', final=True), expected)
        codec = CitationCodec(prompt)
        self.assertEqual(''.join(codec.feed(c) for c in output) + codec.feed('', final=True), expected)

    def test_grouped_ids_unknown_labels_and_incomplete_tokens(self):
        codec = CitationCodec('[[cite:e1]] [[cite:e2]]')
        self.assertEqual(codec.feed('[AIDEA_SOURCE:e1，e2;e1]'), '[[cite:e1]] [[cite:e2]]')
        self.assertEqual(codec.feed('[AIDEA_SOURCE:unknown]'), '[AIDEA_SOURCE:unknown]')
        self.assertEqual(codec.feed('[AIDEA_SOURCE:e1'), '')
        self.assertEqual(codec.feed('', final=True), '[AIDEA_SOURCE:e1')
        codec = CitationCodec('[[cite:e1]]')
        long_text = '[AIDEA_SOURCE:' + 'x' * 1000
        self.assertEqual(codec.feed(long_text) + codec.feed('', final=True), long_text)

    def test_non_citation_chats_and_empty_brackets_remain_literal(self):
        codec = CitationCodec('Ordinary terminal/API chat')
        self.assertEqual(codec.prompt, 'Ordinary terminal/API chat')
        self.assertEqual(codec.feed('[] [AIDEA_SOURCE:e1]'), '[] [AIDEA_SOURCE:e1]')
        codec = CitationCodec('[[cite:e1]]')
        self.assertEqual(codec.feed('No recoverable ID []'), 'No recoverable ID []')


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_bridge_streaming_and_fallback_paths(self):
        for deltas in (['Answer [AIDEA_', 'SOURCE:e1', ']'], []):
            class Client:
                async def generate_content_stream(self, prompt, **kwargs):
                    self_prompt.append(prompt)
                    if deltas:
                        for text in deltas:
                            yield NS(text_delta=text, text='Answer [AIDEA_SOURCE:e1]')
                    else:
                        yield NS(text_delta='', text='Answer [AIDEA_SOURCE:e1]')
            self_prompt = []
            session = NS(settings=Settings(Path('.'), {}), extended=False, client=Client(), check_account=lambda: None)
            bridge = GeminiBridge(session, None)
            events = queue.SimpleQueue()
            await bridge.generate('Document [[cite:e1]]', [], 'model', False, events)
            rows = []
            while not events.empty():
                rows.append(events.get())
            self.assertIn('[AIDEA_SOURCE:e1]', self_prompt[0])
            self.assertNotIn('[[cite:', self_prompt[0])
            self.assertEqual(''.join(value for kind, value in rows if kind == 'text'), 'Answer [[cite:e1]]')
            self.assertEqual(rows[-1][0], 'done')


if __name__ == '__main__':
    unittest.main(verbosity=2)
