from html.parser import HTMLParser
import re
import unittest
from unittest.mock import patch

from src.telegram.formatting import format_reply, units, safe_url


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text, self.code = [], [], []
        self.entity_count = 0

    def handle_starttag(self, tag, attrs):
        self.entity_count += 1
        assert tag in ("b", "i", "s", "a", "code", "pre", "blockquote"), tag
        assert not (tag == "blockquote" and "blockquote" in self.stack)
        assert not (tag == "a" and "a" in self.stack)
        if tag == "a":
            assert not any(parent in ("a", "code", "pre") for parent in self.stack)
        if tag == "blockquote":
            assert not any(parent in ("a", "blockquote", "code", "pre") for parent in self.stack)
        if tag == "pre":
            assert not self.stack
        if tag == "code":
            assert not self.stack or self.stack == ["pre"]
        if "code" in self.stack or "pre" in self.stack:
            assert tag == "code" and self.stack == ["pre"]
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)
        if "code" in self.stack or "pre" in self.stack:
            self.code.append(data)


def visible(chunk):
    if chunk["parse_mode"] is None:
        return chunk["text"]
    parser = TelegramHTML()
    parser.feed(chunk["text"])
    parser.close()
    assert not parser.stack
    return "".join(parser.text)


class FormattingTests(unittest.TestCase):
    def checked(self, source):
        chunks = format_reply(source)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(units(visible(chunk)), 4096)
            self.assertLessEqual(units(chunk["plain_text"]), 4096)
            self.assertTrue(chunk["text"])
            self.assertTrue(set(re.findall(r"&([A-Za-z]+);", chunk["text"])) <= {"amp", "lt", "gt", "quot"})
        return chunks

    def test_cjk_adjacent_strong_closers_render_model_prose(self):
        examples = (
            ('\uff1b', '1 \u6708 20 \u65e5\uff08\u4e09\uff09', '\u4ea6\u6709\u4e00\u5834\u3002'),
            ('\uff0c\u7fcc\u65e5 ', '2 \u6708 25 \u65e5\uff08\u56db\uff09', '\u4ea6\u6709\u4e00\u5834\u3002'),
            ('FC \u6700\u65e9\u512a\u5148\u62bd\u7968\u7531 ', '2026 \u5e74 11 \u6708 20 \u65e5 18:00\uff08\u65e5\u672c\u6642\u9593\uff09',
             '\u958b\u59cb\uff1b\u958b\u5834\u53ca\u958b\u6f14\u6642\u9593\u5c1a\u672a\u516c\u5e03\u3002'),
        )
        for prefix, bold, suffix in examples:
            with self.subTest(bold=bold):
                chunks = self.checked(prefix + '**' + bold + '**' + suffix)
                self.assertEqual(''.join(map(visible, chunks)), prefix + bold + suffix)
                self.assertIn('<b>' + bold + '</b>', ''.join(chunk['text'] for chunk in chunks))

    def test_cjk_strong_preserves_inline_tokens_and_escaped_markers(self):
        source = '**[\u65e5\u7a0b](https://example.com) `**literal** &amp;`\uff09**\u958b\u59cb'
        chunks = self.checked(source)
        output = ''.join(chunk['text'] for chunk in chunks)
        self.assertIn('href="https://example.com"', output)
        self.assertIn('<code>**literal** &amp;amp;</code>', output)
        self.assertTrue(output.endswith('<b>\uff09</b>\u958b\u59cb'))
        self.assertIn('https://example.com', ''.join(chunk['plain_text'] for chunk in chunks))
        for literal in (r'\*\*' + '\u65e5\u7a0b\uff09' + r'\*\*' + '\u958b\u59cb', '**\u65e5\u7a0b\uff09' + r'\*\*' + '\u958b\u59cb',
                        '`**\u65e5\u7a0b\uff09**\u958b\u59cb`', '**English)**word'):
            chunks = self.checked(literal)
            output = ''.join(chunk['text'] for chunk in chunks)
            self.assertNotIn('<b>', output)
            self.assertIn('**', ''.join(map(visible, chunks)))
        ordinary = self.checked('**ordinary** and **\u65e5\u7a0b\uff09**\u958b\u59cb')
        self.assertEqual(ordinary[0]['text'], '<b>ordinary</b> and <b>\u65e5\u7a0b\uff09</b>\u958b\u59cb')

    def test_event_reply_preserves_japanese_links_and_literal_group_name(self):
        source = ('For **=LOVE (イコラブ)** next appearance is **MTV VMAJ 2026**.\n\n'
                  '- **Date:** Thursday, October 29\n- **Venue:** Tokyo Dome\n\n'
                  '[Official details](https://example.com/event?q=love&year=2026) · [Tickets](https://example.com/tickets)')
        first = self.checked(source)
        self.assertEqual(first, format_reply(source))
        self.assertIn('<b>=LOVE (イコラブ)</b>', first[0]["text"])
        self.assertIn('href="https://example.com/event?q=love&amp;year=2026"', first[0]["text"])
        self.assertIn('https://example.com/tickets', first[0]["plain_text"])
        self.assertIn('**=LOVE', source)

    def test_code_and_raw_html_remain_literal_and_nesting_is_telegram_valid(self):
        raw = '  value = "&amp; <b> & <>"  \n\n'
        source = '**before `a < b & c` after**\n\n> quoted\n> ```python\n> ' + raw.replace('\n', '\n> ') + '```\n\n<script>alert(1)</script>'
        chunks = self.checked(source)
        output = ''.join(chunk['text'] for chunk in chunks)
        self.assertIn('<code>a &lt; b &amp; c</code>', output)
        self.assertIn('&lt;script&gt;', output)
        standalone = self.checked('```python\n' + raw + '```')
        parser = TelegramHTML()
        parser.feed(standalone[0]['text'])
        self.assertEqual(''.join(parser.code), raw)
        linked = self.checked('[Use `x`](https://example.com)')
        self.assertIn('https://example.com', linked[0]['plain_text'])
        self.assertIn('<code>x</code>', linked[0]['text'])
        quoted_link = self.checked('> Quoted [source](https://example.com) and **note**')
        self.assertIn('<a href="https://example.com">source</a>', quoted_link[0]['text'])
        self.assertIn('https://example.com', quoted_link[0]['plain_text'])

    def test_entities_escape_once_and_code_entities_are_not_decoded(self):
        chunks = self.checked('&amp; &#65; &#x1F99B; \\&amp; `&amp; <`')
        self.assertEqual(''.join(visible(chunk) for chunk in chunks), '& A \U0001f99b &amp; &amp; <')

    def test_supported_lists_quotes_tables_and_images_keep_content(self):
        source = ('# Heading\n\n3. first\n4. second\n\n- [x] done\n  - [ ] pending\n\n'
                  '> outer\n> > inner\n\n| | Same | Same |\n|---|---|---|\n|zero|one||\n\n![image alt](https://example.com/a.png)')
        chunks = self.checked(source)
        text = ''.join(visible(chunk) for chunk in chunks)
        for value in ('Heading', '3. first', '4. second', '[x] done', '[ ] pending', 'outer', '> inner',
                      'Column 1: zero', 'Same: one', 'Same: ', 'image alt', 'https://example.com/a.png'):
            self.assertIn(value, text)
        self.assertIn('EXTRA', ''.join(visible(chunk) for chunk in self.checked('|A|B|\n|---|---|\n|x|y|EXTRA|')))

    def test_safe_urls_and_language_attributes_do_not_accept_injection(self):
        for url in ('javascript:alert(1)', 'data:text/plain,secret', 'file:///etc/passwd', 'https://user:password@example.com', 'https://example.com\\@evil.example'):
            with self.subTest(scheme=url.split(':')[0]):
                result = self.checked(f'[label]({url})')
                self.assertNotIn('<a ', ''.join(chunk['text'] for chunk in result))
                self.assertIn('label', ''.join(visible(chunk) for chunk in result))
        result = self.checked('```python" onclick="secret\nx < y\n```')
        self.assertNotIn('class=', result[0]['text'])
        self.assertNotIn('onclick=', result[0]['text'])
        for url in ('https://exa<mple.com', 'https://bad_label.example', 'https://-bad.example',
                    'https://example..com', 'https://999.999.1.1', 'https://[::1]evil.example', 'https://example.com/\x80'):
            self.assertFalse(safe_url(url))
        for url in ('https://example.com:8443/path?q=a&x=b', 'http://127.0.0.1:8088',
                    'https://[2001:db8::1]:443/path', 'https://b\u00fccher.example/path'):
            self.assertTrue(safe_url(url), url)

    def test_reference_links_nested_inline_formats_and_bare_urls(self):
        chunks = self.checked('**[a *b* ~~c~~][ref]** https://example.org\n\n[ref]: https://example.com')
        text = chunks[0]['text']
        self.assertIn('<a href="https://example.com"><b>a <i>b</i> <s>c</s></b></a>', text)
        self.assertIn('<a href="https://example.org">', text)

    def test_long_unicode_code_and_links_fit_both_budgets_without_loss(self):
        cluster = '\U0001f469\u200d\U0001f4bb'
        for source in ('hello\n' + '\U0001f99b' * 5000, cluster * 1200, 'e\u0301' * 3000):
            chunks = self.checked(source)
            self.assertEqual(''.join(visible(chunk) for chunk in chunks), source)
            if source.startswith(cluster):
                self.assertTrue(all(visible(chunk).replace(cluster, '') == '' for chunk in chunks))
        for prefix, suffix in (('a' * 4092 + ' ', cluster), ('a' * 4094 + ' ', 'e\u0301')):
            chunks = self.checked(prefix + '**' + suffix + '**')
            self.assertEqual([visible(chunk) for chunk in chunks], [prefix, suffix])
        raw = '  x < y & z  \n' * 700
        chunks = self.checked('```python\n' + raw + '```')
        parsers = []
        for chunk in chunks:
            parser = TelegramHTML()
            parser.feed(chunk['text'])
            parsers.append(parser)
        self.assertEqual(''.join(''.join(parser.code) for parser in parsers), raw)
        links = ' '.join(f'[label {number}](https://example.com/{number}/' + 'x' * 200 + ')' for number in range(40))
        for chunk in self.checked(links):
            self.assertLessEqual(units(chunk['plain_text']), 4096)
        oversized = 'https://example.com/' + 'x' * 6000
        self.assertIn(oversized, ''.join(visible(chunk) for chunk in self.checked(f'[label]({oversized})')))

    def test_unknown_malformed_and_format_only_source_never_disappears(self):
        for source in ('---', '###', '**unfinished', '```\nx < y', '<unknown>literal</unknown>'):
            self.assertTrue(''.join(visible(chunk) for chunk in self.checked(source)))
        with patch('src.telegram.formatting.mistune.Markdown', return_value=lambda _: [{'type': 'future_node'}]):
            result = self.checked('**keep** [everything](https://example.com)')
        self.assertEqual(result[0]['text'], '**keep** [everything](https://example.com)')
        self.assertIsNone(result[0]['parse_mode'])

    def test_expanding_tables_and_many_small_nodes_remain_bounded(self):
        source = '| ' + 'header' * 100 + ' |\n|---|\n' + '|value|\n' * 100
        chunks = self.checked(source)
        self.assertTrue(all(chunk['parse_mode'] is None for chunk in chunks))
        self.assertEqual(''.join(chunk['text'] for chunk in chunks), source)
        many_nodes = self.checked('\\*' * 10000)
        self.assertEqual(''.join(visible(chunk) for chunk in many_nodes), '*' * 10000)
        dense = '*x* ' * 13000
        bounded = self.checked(dense)
        self.assertLess(len(bounded), 128)
        self.assertTrue(all(chunk['parse_mode'] is None for chunk in bounded))
        self.assertEqual(''.join(chunk['text'] for chunk in bounded), dense)

    def test_entity_budget_keeps_all_short_links_and_dense_formatting(self):
        urls = [f'https://a.co/{index}' for index in range(150)]
        source = ' '.join(f'[link]({url})' for url in urls)
        chunks = self.checked(source)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(re.findall(r'href="([^"]+)"', ''.join(chunk['text'] for chunk in chunks)), urls)
        for chunk in chunks + self.checked('*a* **b** ~~c~~ ' * 100):
            parser = TelegramHTML()
            parser.feed(chunk['text'])
            self.assertLessEqual(parser.entity_count, 100)
