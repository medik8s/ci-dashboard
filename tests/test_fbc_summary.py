"""FBC coverage regression tests without starting a web server."""

import ast
from pathlib import Path
import re
import sys
import unittest
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from web.fbc_jobs import (  # noqa: E402
    canonical_fbc_sha, expected_fbc_jobs, FBC_SHA_LENGTH, FBC_SHORT_SHA_LENGTH,
)

SHA = 'a' * FBC_SHA_LENGTH
OTHER_SHA = 'b' * FBC_SHA_LENGTH


def job(version, variant='konflux', name='far-weekly-aws'):
    return f'periodic-ci-openshift-rhwa-system-tests-main-{version}-{variant}-e2e-{name}'


class FbcSummaryTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ROOT / 'config.yaml').read_text())['collector']['prow_gcs']
        self.registry = expected_fbc_jobs(self.config)

    def summary(self, rows, snapshots, version='5.0'):
        # Exercise the actual endpoint body with isolated data dependencies.
        tree = ast.parse((ROOT / 'src/web/server.py').read_text())
        function = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == 'api_fbc_summary')
        function.decorator_list = []
        namespace = {
            're': re,
            'request': SimpleNamespace(args=SimpleNamespace(
                get=lambda key, default=None, **kwargs: version if key == 'version' else default)),
            'normalize_version': lambda value: value,
            'db': SimpleNamespace(get_job_run_history=lambda **kwargs: rows),
            'fbc_jobs': self.registry,
            'fbc_app_for_version': lambda v: ('rhwa-fbc-' + v.replace('.', '').ljust(3, '0')) if v else None,
            '_FBC_APP_RE': re.compile(r'rhwa-fbc-\d+'),
            '_list_recent_snapshots': lambda app, **kwargs: snapshots.get(app, []),
            '_build_fbc_urls': lambda image: {},
            '_build_log_urls': lambda *args, **kwargs: {},
            'QUAY_FBC_REPO_PREFIX': 'tenant/fbc',
            'canonical_fbc_sha': canonical_fbc_sha,
            'FBC_SHA_LENGTH': FBC_SHA_LENGTH,
            'FBC_SHORT_SHA_LENGTH': FBC_SHORT_SHA_LENGTH,
            'jsonify': lambda value: value,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<endpoint>', 'exec'), namespace)
        return namespace['api_fbc_summary']()['fbc_summaries']

    def snapshot(self, sha=SHA):
        return {'commit_sha': sha, 'snapshot_name': 'snapshot-' + sha,
                'created': '2026-01-01T10:00:00Z'}

    def row(self, version='5.0', sha=SHA, job_name=None):
        app = '500' if version == '5.0' else '422'
        return {'job_name': job_name or job(version), 'build_id': '123',
                'version': version, 'fbc_image': f'quay.io/tenant/rhwa-fbc-{app}:{sha}',
                'run_date': '2026-01-02T10:00:00Z', 'step_name': 'e2e-far-weekly-aws',
                'status': 'passed'}

    def test_registry_excludes_non_fbc_and_has_exact_schedules(self):
        self.assertEqual(len(self.registry['5.0']), 13)
        all_jobs = {name for jobs in self.registry.values() for name in jobs}
        self.assertEqual(all_jobs, set(self.config['fbc_schedule_overrides']))
        disconnected = job('5.0', 'disconnected', 'far-weekly-aws-disconnected')
        self.assertEqual(self.registry['5.0'][disconnected]['schedule'], 'Fri 00:00 UTC')
        self.assertEqual(self.registry['4.22'][job('4.22')]['schedule'], 'Tue 04:00 UTC')

    def test_empty_history_lists_all_expected_jobs(self):
        summary = self.summary([], {'rhwa-fbc-500': [self.snapshot()]})[0]
        self.assertEqual((summary['total'], summary['expected_total'], summary['not_run']), (0, 13, 13))
        self.assertEqual(len(summary['expected_jobs']), 13)
        self.assertEqual(self.summary([], {}), [])

    def test_other_snapshot_and_missing_metadata_do_not_satisfy_coverage(self):
        missing = {**self.row(), 'fbc_image': None}
        summaries = self.summary([self.row(sha=OTHER_SHA), missing],
                                 {'rhwa-fbc-500': [self.snapshot()]})
        selected = next(s for s in summaries if s['fbc_full'] == SHA)
        self.assertEqual(selected['not_run'], 13)
        observed = next(s for s in summaries if s['fbc_full'] == OTHER_SHA)
        self.assertEqual((observed['passed'], observed['total'], observed['not_run']), (1, 1, 12))

    def test_same_commit_and_suffix_are_separate_across_versions(self):
        summaries = self.summary([self.row(), self.row('4.22')], {}, version='')
        self.assertEqual(len(summaries), 2)
        self.assertEqual(len({s['summary_id'] for s in summaries}), 2)
        for summary in summaries:
            self.assertTrue(all(f"-{summary['version']}-" in j['job_name']
                                for j in summary['expected_jobs']))

    def test_unique_short_tag_joins_full_snapshot(self):
        summaries = self.summary([self.row(sha=SHA[:FBC_SHORT_SHA_LENGTH])],
                                 {'rhwa-fbc-500': [self.snapshot()]})
        self.assertEqual(len(summaries), 1)
        self.assertEqual((summaries[0]['fbc_full'], summaries[0]['total']), (SHA, 1))
        self.assertTrue(summaries[0]['can_trigger'])
        self.assertEqual(summaries[0]['snapshot_name'], 'snapshot-' + SHA)

    def test_ambiguous_or_unresolved_prefix_remains_separate(self):
        prefix = SHA[:FBC_SHORT_SHA_LENGTH]
        alternative = prefix + 'c' * (FBC_SHA_LENGTH - FBC_SHORT_SHA_LENGTH)
        self.assertEqual(canonical_fbc_sha(prefix, {SHA, alternative}), prefix)
        summary = self.summary([self.row(sha=prefix)], {})[0]
        self.assertFalse(summary['can_trigger'])

    def test_unexpected_observed_job_does_not_reduce_not_run(self):
        summary = self.summary([self.row(job_name=job('5.0', name='far-extra-aws'))], {})[0]
        self.assertEqual((summary['total'], summary['not_run']), (1, 13))

    def test_unknown_application_keeps_observed_run_without_expected_jobs(self):
        row = {**self.row(), 'version': '', 'fbc_image': 'quay.io/other/catalog:' + SHA}
        summary = self.summary([row], {}, version='')[0]
        self.assertEqual(summary['app_name'], 'unknown')
        self.assertEqual((summary['total'], summary['expected_total'], summary['not_run']), (1, 0, 0))

    def test_latest_run_wins_for_same_full_job(self):
        old = {**self.row(), 'run_date': '2026-01-01T10:00:00Z', 'status': 'failed'}
        summary = self.summary([old, self.row()], {})[0]
        self.assertEqual((summary['total'], summary['passed'], summary['failed']), (1, 1, 0))


if __name__ == '__main__':
    unittest.main()
