import contextlib
import copy
import datetime as dt
import email.utils
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import radar as r

NOW = dt.datetime(2026, 10, 30, 16, 0, tzinfo=r.UTC)


def item(title='Messi faces racism backlash', source='Reuters', host='reuters.com', age=10, **extra):
    return dict(title=title, source=source, source_url='https://' + host,
                url='https://news.google.com/rss/articles/' + r.key_for({'title': title}),
                published=NOW-dt.timedelta(minutes=age), kind='news', **extra)


def state():
    return dict(r.empty_state(), initialized=True)


class RadarTests(unittest.TestCase):
    def run_process(self, items, st=None, **kwargs):
        st = st if st is not None else state()
        with patch.object(r, 'send_message') as send:
            result = r.process(items, st, NOW, **kwargs)
        return result, st, send

    def test_familiar_name_is_not_enough(self):
        for title in ('Messi interview', 'Messi wins award', 'Messi signs new contract', 'Messi viral video'):
            self.assertIsNone(r.candidate(item(title), NOW))

    def test_unknown_people_with_roles(self):
        self.assertEqual(r.get_entity('Singer Jane Smith apologizes after racism backlash'), 'Jane Smith')
        self.assertEqual(r.get_entity('Singer Jane Smith Apologizes After Racism Backlash'), 'Jane Smith')
        self.assertEqual(r.get_entity('歌手陈小明公开道歉引发争议'), '陈小明')
        self.assertIsNone(r.get_entity('Acme Phone controversy'))
        self.assertIsNotNone(r.candidate(item('Singer Jane Smith apologizes after racism backlash'), NOW))

    def test_old_future_and_retrospective(self):
        for it in (item(age=121), item(age=-6), item('Messi racism backlash resurfaces'),
                   item('Messi去年争议回顾'), item('Messi racism controversy last year')):
            self.assertIsNone(r.candidate(it, NOW))
        self.assertIsNotNone(r.candidate(item(age=119), NOW))

    def test_demand_is_not_apology(self):
        for title in ('Fans demand Messi apology for racism', 'Calls for Messi to apologize for racism',
                      'Messi refuses to apologize', '要求梅西道歉', '梅西尚未道歉'):
            self.assertNotEqual(r.event_type(title), '道歉回应', title)
        self.assertEqual(r.event_type('Messi apologizes over racism scandal'), '道歉回应')

    def test_untrusted_and_spoofed_sources(self):
        for host in ('random-blog.test', 'reuters.com.evil.test', 'news.google.com'):
            result, _, send = self.run_process([item('Messi apologizes over racism backlash', host=host)])
            self.assertEqual(result[0], 0)
            send.assert_not_called()
        self.assertFalse(r.publisher(item(host='reuters.com.evil.test'))[1])

    def test_trusted_single_concrete_fresh_report(self):
        result, _, send = self.run_process([item('Messi apologizes over racism backlash')])
        self.assertEqual(result[0], 1)
        self.assertIn('尚未独立核实', send.call_args.args[0])

    def test_controversy_requires_corroboration(self):
        self.assertEqual(self.run_process([item()])[0][0], 0)
        items = [item(), item('Racism scandal engulfs Messi', source='BBC', host='bbc.com')]
        self.assertEqual(self.run_process(items)[0][0], 1)

    def test_duplicate_feed_regional_domain_and_syndication(self):
        for items in (
            [item(), item()],
            [item(), item('Messi racism controversy grows', source='Reuters UK', host='uk.reuters.com')],
            [item(), item(source='BBC', host='bbc.com')],
            [item('Reuters: Messi racism backlash'), item('Reuters: racism controversy for Messi', host='bbc.com')],
        ):
            self.assertEqual(self.run_process(items)[0][0], 0)

    def test_paraphrases_across_runs_do_not_resend(self):
        st = state()
        self.assertEqual(self.run_process([item('Messi apologizes over racism backlash')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('Apology from Messi amid racial controversy', host='bbc.com')], st)[0][0], 0)

    def test_real_progress_does_resend_without_person_cooldown(self):
        st = state()
        initial = [item(), item('Messi racial controversy grows', host='bbc.com')]
        self.assertEqual(self.run_process(initial, st)[0][0], 1)
        self.assertEqual(self.run_process([item('Messi apologizes for racism controversy')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('Messi arrested over tax evasion')], st)[0][0], 1)

    def test_different_brand_events_not_merged(self):
        st = state()
        self.assertEqual(self.run_process([item('Nike drops Messi after controversy')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('Adidas drops Messi after controversy')], st)[0][0], 1)

    def test_cross_language_topic_matching(self):
        st = state()
        self.assertEqual(self.run_process([item('Messi apologizes for racism')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('梅西因种族歧视争议公开道歉', host='bbc.com')], st)[0][0], 0)

    def test_corrob_across_runs(self):
        st = state()
        self.assertEqual(self.run_process([item()], st)[0][0], 0)
        self.assertEqual(self.run_process([item('Messi racism controversy grows', host='bbc.com')], st)[0][0], 1)

    def test_unrelated_old_stage_does_not_confirm_apology(self):
        st = state()
        self.run_process([item(), item('Messi racial controversy', host='bbc.com')], st)
        self.assertEqual(self.run_process([item('Messi reportedly apologizes over racism')], st)[0][0], 0)

    def test_rumor_and_opinion(self):
        for title in ('Messi reportedly apologizes over racism', 'Messi alleged racism scandal'):
            self.assertEqual(self.run_process([item(title)])[0][0], 0)
        self.assertIsNone(r.candidate(item('Opinion: Messi should apologize for racism'), NOW))
        reports = [item('Messi alleged racism scandal'), item('Messi alleged racial controversy', host='bbc.com')]
        _, _, send = self.run_process(reports)
        self.assertIn('尚未证实', send.call_args.args[0])
        self.assertIn('先存疑', send.call_args.args[0])

    def test_freshness_ranking_and_limit(self):
        reports = [item('Messi apologizes for racism', age=40), item('Mbappe arrested over tax evasion', age=3),
                   item('Taylor Swift apologizes for interview scandal', age=2)]
        result, _, send = self.run_process(reports)
        self.assertEqual(result[0], 2)
        self.assertIn('泰勒斯威夫特', send.call_args_list[0].args[0])

    def test_baseline_and_state_migration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'STATE_PATH', Path(tmp)/'state.json'):
            r.STATE_PATH.write_text(json.dumps({'initialized': True, 'seen': ['legacy'], 'cooldowns': {'a': 'b'}}))
            st = r.load_state()
            result, st, send = self.run_process([item('Messi apologizes for racism')], st)
            self.assertEqual(result[0], 0)
            send.assert_not_called()
            self.assertIn('legacy', st['seen'])
            self.assertEqual(st['version'], 2)
            self.assertNotIn('cooldowns', st)
            self.assertEqual(self.run_process([item('Messi apologizes for racism')], st)[0][0], 0)
            r.save_state(st)
            self.assertEqual(r.load_state(), st)

    def test_state_atomic_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'STATE_PATH', Path(tmp)/'state.json'):
            self.assertFalse(r.load_state()['initialized'])
            r.save_state(state())
            self.assertFalse(r.STATE_PATH.with_suffix('.tmp').exists())
            r.STATE_PATH.write_text('{broken')
            with self.assertRaises(RuntimeError):
                r.load_state()
            self.assertEqual(r.STATE_PATH.read_text(), '{broken')

    def test_delivery_failure_checkpoints_successful_prefix(self):
        st = state()
        checkpoints = []
        reports = [item('Messi apologizes for racism'), item('Mbappe arrested over tax evasion', age=5)]
        with patch.object(r, 'send_message', side_effect=[None, RuntimeError('failure')]):
            with self.assertRaises(RuntimeError):
                r.process(reports, st, NOW, checkpoint=lambda s: checkpoints.append(copy.deepcopy(s)))
        self.assertEqual(len(checkpoints), 1)
        saved = checkpoints[0]
        result, _, send = self.run_process(reports, saved)
        self.assertEqual(result[0], 1)
        self.assertIn('梅西', send.call_args.args[0])

    def test_html_layout_escape_and_safe_links(self):
        it = item('梅西公开道歉 <script>&"', source='A&B')
        text = r.render_alert(it, '梅西')
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('A&amp;B</a>', text)
        self.assertNotIn('来源链接：https://', text)
        self.assertIn('事件发生时间：RSS 未提供', text)
        self.assertLess(len(text), 4096)
        self.assertNotIn('href=', r.render_alert(dict(it, url='javascript:alert(1)'), '梅西'))

    def test_send_transport_uses_html_and_no_preview(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"ok":true}'
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test-placeholder', 'TELEGRAM_CHAT_ID': 'test-chat'}), patch.object(r, 'urlopen', return_value=Response()) as request:
            r.send_message('<b>test</b>')
        data = parse_qs(request.call_args.args[0].data.decode())
        self.assertEqual(data['parse_mode'], ['HTML'])
        self.assertEqual(data['disable_web_page_preview'], ['true'])

    def test_news_and_trends_parsing(self):
        date = email.utils.format_datetime(NOW)
        news = f'<rss><channel><item><title>Messi apologizes - Reuters</title><link>https://example.test/a</link><pubDate>{date}</pubDate><source url="https://reuters.com">Reuters</source></item></channel></rss>'
        parsed = r.parse_news(news.encode())[0]
        self.assertEqual(parsed['title'], 'Messi apologizes')
        self.assertTrue(r.publisher(parsed)[1])
        xml = f'<rss xmlns:ht="{r.HTNS}"><channel><item><title>Messi</title><pubDate>{date}</pubDate><ht:news_item><ht:news_item_title>Messi apologizes</ht:news_item_title></ht:news_item></item></channel></rss>'
        self.assertEqual(r.parse_trends(xml.encode(), 'US')[0]['related'][0]['title'], 'Messi apologizes')

    def test_fetch_all_sources_and_partial_failure(self):
        date = email.utils.format_datetime(NOW)
        xml = f'<rss><channel><item><title>Messi</title><pubDate>{date}</pubDate></item></channel></rss>'.encode()
        with patch.object(r, 'http_get', return_value=xml):
            items, successes = r.fetch_sources()
        self.assertEqual((len(items), successes), (10, 10))
        with patch.object(r, 'http_get', side_effect=lambda url: xml if 'news.google' in url else (_ for _ in ()).throw(RuntimeError())):
            items, successes = r.fetch_sources()
        self.assertEqual((len(items), successes), (6, 6))

    def test_dry_run_does_not_send_or_save(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'STATE_PATH', Path(tmp)/'state.json'):
            r.save_state(state())
            before = r.STATE_PATH.read_bytes()
            with patch.object(r, 'fetch_sources', return_value=([item('Messi apologizes for racism')], 1)), patch.object(r, 'now_utc', return_value=NOW), patch('sys.argv', ['radar.py', '--dry-run']), patch.object(r, 'send_message') as send, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(r.main(), 0)
                send.assert_not_called()
            self.assertEqual(before, r.STATE_PATH.read_bytes())

    def test_no_news_or_all_sources_failure_preserves_state(self):
        for items, successes in (([], 0), ([dict(item(), kind='trend')], 1)):
            with patch.object(r, 'load_state', return_value=state()), patch.object(r, 'fetch_sources', return_value=(items, successes)), patch.object(r, 'save_state') as save, patch('sys.argv', ['radar.py']):
                self.assertEqual(r.main(), 1)
                save.assert_not_called()

    def test_test_and_dry_run_exclusive(self):
        with patch('sys.argv', ['radar.py', '--test', '--dry-run']), patch.object(r, 'send_message') as send, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): r.main()
            send.assert_not_called()


    def test_album_release_and_hypothetical_are_not_brand_termination(self):
        self.assertIsNone(r.candidate(item('Singer Jane Smith drops new album'), NOW))
        self.assertIsNone(r.candidate(item('Will Messi apologize for racism?'), NOW))
        self.assertEqual(r.event_type('Messi denies apologizing over racism'), '公开回应')

    def test_arrest_then_verdict_is_important_progress(self):
        st = state()
        self.assertEqual(self.run_process([item('Messi arrested for tax evasion')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('Messi convicted of tax evasion')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('Tax evasion verdict against Messi')], st)[0][0], 0)

    def test_new_progress_suppresses_older_initial_controversy(self):
        reports = [item(age=20), item('Messi racism controversy', host='bbc.com', age=15), item('Messi apologizes over racism', age=2)]
        result, st, send = self.run_process(reports)
        self.assertEqual(result[0], 1)
        self.assertIn('道歉回应', send.call_args.args[0])
        self.assertEqual(self.run_process(reports, st)[0][0], 0)

    def test_brand_conflict_even_with_shared_racism_topic(self):
        st = state()
        self.assertEqual(self.run_process([item('Nike drops Messi over racism')], st)[0][0], 1)
        self.assertEqual(self.run_process([item('Adidas drops Messi over racism')], st)[0][0], 1)

    def test_untrusted_duplicate_cannot_hide_trusted_report(self):
        reports = [item('Messi apologizes for racism', host='blog.test'), item('Messi apologizes for racism')]
        result, st, _ = self.run_process(reports)
        self.assertEqual(result[0], 1)
        self.assertTrue(st['events'][0]['reports'][0]['trusted'])

    def test_no_automatic_message_retry_after_timeout(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test-placeholder', 'TELEGRAM_CHAT_ID': 'test-chat'}), patch.object(r, 'urlopen', side_effect=TimeoutError('sensitive-url')) as request:
            with self.assertRaises(RuntimeError) as err:
                r.send_message('test')
        self.assertNotIn('sensitive-url', str(err.exception))
        self.assertEqual(request.call_count, 1)

    def test_future_state_schema_is_not_silently_reinitialized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'STATE_PATH', Path(tmp)/'state.json'):
            r.STATE_PATH.write_text(json.dumps(dict(state(), version=99)))
            with self.assertRaises(RuntimeError): r.load_state()

    def test_late_corroboration_does_not_rescue_expired_report(self):
        st = state()
        self.run_process([item(age=110)], st)
        with patch.object(r, 'send_message') as send:
            result = r.process([item('Messi racism controversy', host='bbc.com', age=10)], st, NOW+dt.timedelta(minutes=20))
        self.assertEqual(result[0], 0)
        send.assert_not_called()

    def test_event_ttl_does_not_refresh_on_rewrites(self):
        st = state()
        self.run_process([item('Messi apologizes for racism')], st)
        first_seen = st['events'][0]['first_seen']
        self.run_process([item('Messi apology for racial scandal', host='bbc.com', age=1)], st)
        self.assertEqual(st['events'][0]['first_seen'], first_seen)

    def test_saved_source_link_available_to_later_corroboration(self):
        st = state()
        self.run_process([item(source='Reuters')], st)
        _, _, send = self.run_process([item('Messi racial controversy grows', host='bbc.com', source='BBC')], st)
        self.assertIn('Reuters</a>', send.call_args.args[0])
        self.assertIn('BBC</a>', send.call_args.args[0])

    def test_trend_only_boost_matches_specific_linked_title(self):
        news = item('Messi apologizes for racism')
        trend = dict(kind='trend', title='Messi', published=NOW, related=[{'title': news['title']}])
        _, _, send = self.run_process([news, trend])
        self.assertIn('进入 Google 趋势', send.call_args.args[0])
        trend['related'] = [{'title': 'Messi unrelated injury update'}]
        _, _, send = self.run_process([news, trend])
        self.assertNotIn('进入 Google 趋势', send.call_args.args[0])


    def test_verdict_does_not_replay_earlier_arrest_same_batch(self):
        result, st, send = self.run_process([item('Messi arrested for tax evasion', age=30), item('Messi convicted of tax evasion', age=2)])
        self.assertEqual(result[0], 1)
        self.assertIn('convicted', send.call_args.args[0])
        self.assertIn('法律进展:被捕', st['events'][0]['stages'])

    def test_missing_dates_cannot_be_recent(self):
        self.assertIsNone(r.candidate(dict(item(), published=None), NOW))
        self.assertIsNone(r.parse_when('not a date'))

    def test_changed_observations_saved_without_alerts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'STATE_PATH', Path(tmp)/'state.json'):
            r.save_state(state())
            with patch.object(r, 'fetch_sources', return_value=([item()], 1)), patch.object(r, 'now_utc', return_value=NOW), patch('sys.argv', ['radar.py']), patch.object(r, 'send_message') as send:
                self.assertEqual(r.main(), 0)
                send.assert_not_called()
            saved = r.load_state()
            self.assertEqual(len(saved['events'][0]['reports']), 1)
            self.assertEqual(saved['last_run'], email.utils.format_datetime(NOW))

    def test_event_with_corrupted_report_fails_closed(self):
        _, st, _ = self.run_process([item('Messi apologizes for racism')])
        st['events'][0]['reports'][0]['published'] = 'broken'
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'STATE_PATH', Path(tmp)/'state.json'):
            r.STATE_PATH.write_text(json.dumps(st))
            with self.assertRaises(RuntimeError): r.load_state()


if __name__ == '__main__':
    unittest.main()
