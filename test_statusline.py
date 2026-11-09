import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import statusline as status


class MagpieStatusTests(unittest.TestCase):
    NOW = 1700000000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.root = Path(self.temp.name)
        self.gateway = self.root / 'magpie'
        self.gateway.mkdir()
        self.ledger = self.gateway / 'usage.jsonl'
        self.settings = self.root / 'settings.json'
        self.settings.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:3425'}}))
        self.accounts = {'accounts': {'personal': {'email': 'me@gmail.com', 'label': 'personal'}}}
        for name, value in {
            'MAGPIE_DIR': str(self.gateway),
            'MAGPIE_CACHE_DIR': str(self.root / 'session-cache'),
            'SETTINGS_PATH': str(self.settings),
            'ACCOUNTS_PATH': str(self.root / 'accounts.json'),
        }.items():
            self.patches.enter_context(patch.object(status, name, value))
        self.patches.enter_context(patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:3425'}))
        self.payload = {'session_id': 'session-a', 'cwd': '/tmp/project',
                        'model': {'id': 'claude/claude-opus-5-5[1m]',
                                  'display_name': 'Claude Opus 5.5 (1M context)'},
                        'effort': {'level': 'xhigh'}, 'context_window': {'used_percentage': 23}}

    def record(self, session='session-a', user='work@example.com · WorldEngine',
               provider='claude', model='claude-opus-5-5', **extra):
        return dict(session=session, providerAccount=user, provider=provider,
                    model=model, served=model, effort='low', **extra)

    def write_records(self, *records):
        self.ledger.write_text(''.join(json.dumps(r) + '\n' for r in records))

    def write_quotas(self):
        def entry(*windows):
            return {'quota': {'windows': [{'name': name, 'used': used, 'resetsAt': reset}
                                         for name, used, reset in windows]}}
        (self.gateway / 'quotas.json').write_text(json.dumps({
            'claude/work@example.com': entry(('7 days', 99, self.NOW + 86400)),
            'claude/work@example.com · worldengine': entry(
                ('5 hours', 12, self.NOW + 3600), ('7 days', 4, self.NOW + 86400)),
            'claude/me@gmail.com': entry(('5 hours', 0, None), ('7 days', 99, self.NOW + 86400)),
            'codex/work@example.com': entry(('7 days', 63, self.NOW + 86400)),
        }))

    def snapshot(self):
        return status.magpie_state(self.payload, self.accounts, self.NOW)

    def test_sessions_do_not_borrow_the_most_recent_other_accounts(self):
        self.write_records(self.record(), self.record(session='session-b', user='me@gmail.com'))
        self.write_quotas()
        state = self.snapshot()
        self.assertEqual(state['account'], 'WorldEngine')
        self.assertEqual(state['usage']['seven_day']['used_percentage'], 4)
        self.assertEqual(state['model'], 'Claude Opus 5.5 (1M context)')

    def test_fallback_changes_model_account_and_windows_together(self):
        self.write_records(self.record(), self.record(provider='codex', user='work@example.com', model='gpt-6.1-sol'))
        self.write_quotas()
        state = self.snapshot()
        self.assertEqual(state['model'], 'GPT-6.1-Sol')
        self.assertEqual(state['account'], 'Codex')
        self.assertEqual(state['windows'], ['seven_day'])
        self.assertEqual(state['usage']['seven_day']['used_percentage'], 63)

    def test_personal_fallback_uses_personal_quota(self):
        self.write_records(self.record(user='me@gmail.com'))
        self.write_quotas()
        state = self.snapshot()
        self.assertEqual(state['account'], 'Personal')
        self.assertEqual(state['usage']['seven_day']['used_percentage'], 99)
        self.assertNotIn('five_hour', state['usage'])
        self.assertTrue(state['seen']['five_hour'])

    def test_native_session_identity_takes_precedence_over_internal_session(self):
        self.write_records(self.record(session='bridge-session', native_session='session-a'),
                           self.record(native_session='different-cli', user='me@gmail.com'))
        self.assertEqual(self.snapshot()['account'], 'WorldEngine')

    def test_no_record_never_guesses_from_global_login(self):
        self.write_records(self.record(session='other-session', user='me@gmail.com'))
        state = self.snapshot()
        self.assertEqual(state['account'], 'Magpie…')
        self.assertIsNone(state['model'])
        self.assertEqual(state['windows'], [])

    def test_incomplete_trailing_record_is_retried_after_append(self):
        self.write_records(self.record())
        next_record = json.dumps(self.record(user='me@gmail.com'))
        with self.ledger.open('a') as handle:
            handle.write(next_record[:-3])
        self.assertEqual(self.snapshot()['account'], 'WorldEngine')
        with self.ledger.open('a') as handle:
            handle.write(next_record[-3:] + '\n')
        self.assertEqual(self.snapshot()['account'], 'Personal')

    def test_idle_session_cache_survives_ledger_rotation(self):
        self.write_records(self.record())
        self.assertEqual(self.snapshot()['account'], 'WorldEngine')
        self.ledger.unlink()
        self.write_records(self.record(session='busy-session', user='me@gmail.com'))
        self.assertEqual(self.snapshot()['account'], 'WorldEngine')

    def test_explicit_native_endpoint_overrides_gateway_settings(self):
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}):
            self.assertIsNone(self.snapshot())

    def test_bounded_tail_skips_partial_first_record(self):
        self.write_records(self.record(session='long-other', filler='x' * 1000), self.record())
        with patch.object(status, 'MAGPIE_LOG_BYTES', 512):
            self.assertEqual(self.snapshot()['account'], 'WorldEngine')

    def test_gateway_render_does_not_read_native_credentials_or_poll(self):
        self.write_records(self.record())
        self.write_quotas()
        stream = io.StringIO()
        with patch.object(status, 'track_session', side_effect=AssertionError('native credentials read')), \
             patch.object(status, 'spawn_poll', side_effect=AssertionError('OAuth poll started')), \
             patch.object(status.time, 'time', return_value=self.NOW), \
             patch.object(status.sys, 'argv', ['statusline.py']), \
             patch.object(status.sys, 'stdin', io.StringIO(json.dumps(self.payload))), \
             patch.dict(os.environ, {'COLUMNS': '120'}), contextlib.redirect_stdout(stream):
            status.main()
        text = status.ANSI.sub('', stream.getvalue())
        self.assertIn('WorldEngine', text)
        self.assertIn('Opus 5.5 1M', text)
        self.assertIn('12%', text)
        self.assertNotIn('99%', text)

    def test_identity_context_and_both_numeric_quotas_survive_narrow_panes(self):
        self.write_records(self.record())
        self.write_quotas()
        state = self.snapshot()
        info = dict(state, folder='project', branch='feature-long-branch', context=23)
        for columns in range(28, 141):
            line = status.render_status(info, self.NOW, columns)
            self.assertLessEqual(status.printed_width(line), columns)
            text = status.ANSI.sub('', line)
            self.assertRegex(text, r'\b(?:WE|WorldEngine)\b')
            self.assertRegex(text, r'(?:Opus\s*5\.5|O5\.5)')
            self.assertRegex(text, r'(?:ctx|c)23%')
            self.assertRegex(text, r'5h\s*12%')
            self.assertRegex(text, r'wk\s*4%')
            self.assertNotRegex(text, '[█░]')

    def test_narrow_panes_keep_high_usage_values_in_full(self):
        info = dict(folder='project', branch='feature', model='Opus 5.5 1M', account='WorldEngine',
                    effort='xhigh', context=100, windows=['five_hour', 'seven_day'],
                    usage={name: {'used_percentage': 100, 'resets_at': self.NOW + 3600}
                           for name in ['five_hour', 'seven_day']},
                    seen={'five_hour': True, 'seven_day': True})
        for columns in [28, 32, 38, 50, 80, 125]:
            line = status.render_status(info, self.NOW, columns)
            text = status.ANSI.sub('', line)
            self.assertLessEqual(status.printed_width(line), columns)
            self.assertRegex(text, r'5h\s*100%')
            self.assertRegex(text, r'wk\s*100%')
            self.assertRegex(text, r'(?:ctx|c)100%')

    def test_codex_panes_keep_the_actual_model_and_only_the_available_window(self):
        self.write_records(self.record(provider='codex', user='work@example.com', model='gpt-6.1-sol'))
        self.write_quotas()
        info = dict(self.snapshot(), folder='project', branch='feature', context=23)
        for columns in [28, 38, 60, 125]:
            line = status.render_status(info, self.NOW, columns)
            text = status.ANSI.sub('', line)
            self.assertLessEqual(status.printed_width(line), columns)
            self.assertIn('CX' if columns < 110 else 'Codex', text)
            self.assertRegex(text, r'(?:GPT-?6\.1-Sol|G6\.1S)')
            self.assertRegex(text, r'wk\s*63%')
            self.assertNotIn('5h', text)

    def test_medium_and_narrow_panes_remove_optional_metadata(self):
        self.write_records(self.record())
        self.write_quotas()
        info = dict(self.snapshot(), folder='long-project-name', branch='long-feature-branch', context=23)
        medium = status.ANSI.sub('', status.render_status(info, self.NOW, 64))
        narrow = status.ANSI.sub('', status.render_status(info, self.NOW, 38))
        for text in [medium, narrow]:
            self.assertNotIn('long-project-name', text)
            self.assertNotIn('long-feature-branch', text)
            self.assertNotIn('1M', text)
            self.assertNotIn('·', text)
        self.assertNotIn('low', narrow)
        self.assertNotIn(' l ', narrow)

    def test_unicode_metadata_respects_terminal_cell_width(self):
        self.write_records(self.record())
        self.write_quotas()
        info = dict(self.snapshot(), folder='播放器界面', branch='修复播放状态同步和字幕显示', context=23)
        for columns in [38, 80, 125]:
            line = status.render_status(info, self.NOW, columns)
            self.assertLessEqual(status.printed_width(line), columns)
            self.assertRegex(status.ANSI.sub('', line), r'wk\s*4%')

    def test_original_colors_remain_visible_with_numeric_adaptive_layouts(self):
        self.write_records(self.record())
        self.write_quotas()
        info = dict(self.snapshot(), folder='project', branch='feature-branch', context=23)
        for columns in [38, 125]:
            line = status.render_status(info, self.NOW, columns)
            self.assertIn(status.CYAN, line)
            self.assertIn(status.BLUE, line)
            self.assertIn(status.GREEN + '23%', line)
            self.assertIn(status.GREEN + '12%', line)
            self.assertLessEqual(status.printed_width(line), columns)
        wide = status.render_status(info, self.NOW, 125)
        self.assertIn(status.MAGENTA + 'feature-branch', wide)
        self.assertIn(status.CYAN + status.BOLD + 'project', wide)


if __name__ == '__main__':
    unittest.main()
