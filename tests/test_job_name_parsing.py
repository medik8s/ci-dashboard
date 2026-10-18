"""Job-name parsing regression tests for the Prow GCS collector.

Covers _derive_step_name and _extract_operator for every tracked job
naming scheme: system-tests variants (konflux/upgrade/disconnected/
upstream), TLS scanner jobs, and the dev-repo e2e-system-tests targets
that run the shared suites against operator PRs.
"""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from collectors.prow_gcs import ProwGCSCollector  # noqa: E402


class JobNameParsingTests(unittest.TestCase):
    def setUp(self):
        # Both helpers are pure functions of the job name; skip the
        # config-heavy __init__.
        self.collector = ProwGCSCollector.__new__(ProwGCSCollector)

    def test_system_tests_variants_keep_step_suffix(self):
        cases = {
            'periodic-ci-openshift-rhwa-system-tests-main-4.22-konflux-e2e-far-weekly-aws':
                'e2e-far-weekly-aws',
            'periodic-ci-openshift-rhwa-system-tests-main-4.22-upgrade-e2e-far-upgrade-aws':
                'e2e-far-upgrade-aws',
            'periodic-ci-openshift-rhwa-system-tests-main-4.22-disconnected-e2e-far-nhc-interop-weekly-aws-disconnected':
                'e2e-far-nhc-interop-weekly-aws-disconnected',
            'periodic-ci-openshift-rhwa-system-tests-main-5.0-upstream-e2e-sbr-daily-aws-odf':
                'e2e-sbr-daily-aws-odf',
        }
        for job_name, expected in cases.items():
            self.assertEqual(self.collector._derive_step_name(job_name), expected)

    def test_dev_repo_system_tests_targets_are_step_names(self):
        cases = {
            'pull-ci-medik8s-storage-based-remediation-main-5.0-e2e-system-tests-sbr-pr-aws-odf':
                'e2e-system-tests-sbr-pr-aws-odf',
            'pull-ci-medik8s-fence-agents-remediation-main-5.0-e2e-system-tests-far-fbc-upgrade':
                'e2e-system-tests-far-fbc-upgrade',
            'pull-ci-medik8s-self-node-remediation-main-5.0-e2e-system-tests-snr-upstream-pr-upgrade':
                'e2e-system-tests-snr-upstream-pr-upgrade',
        }
        for job_name, expected in cases.items():
            self.assertEqual(self.collector._derive_step_name(job_name), expected)

    def test_tls_jobs_keep_scanner_step_name(self):
        self.assertEqual(
            self.collector._derive_step_name(
                'periodic-ci-openshift-rhwa-system-tests-main-5.0-tls13-adherence-all'),
            'tls13-adherence-all')
        self.assertIsNone(
            self.collector._derive_step_name('pull-ci-medik8s-unknown-repo-main-images'))

    def test_operator_extraction_covers_all_naming_schemes(self):
        cases = {
            'periodic-ci-openshift-rhwa-system-tests-main-4.22-konflux-e2e-sbr-weekly-aws-odf': 'SBR',
            'pull-ci-openshift-rhwa-system-tests-main-4.22-disconnected-e2e-far-nhc-interop-aws-disconnected': 'FAR',
            'pull-ci-medik8s-storage-based-remediation-main-5.0-e2e-system-tests-sbr-pr-aws-odf': 'SBR',
            'pull-ci-medik8s-fence-agents-remediation-main-5.0-e2e-system-tests-far-upstream-pr-upgrade': 'FAR',
            'pull-ci-medik8s-node-healthcheck-operator-main-5.0-e2e-system-tests-nhc-fbc-upgrade': 'NHC',
            'pull-ci-medik8s-machine-deletion-remediation-main-5.0-e2e-system-tests-mdr-pr-aws': 'MDR',
            'pull-ci-medik8s-node-maintenance-operator-main-5.0-e2e-system-tests-nmo-fbc-upgrade': 'NMO',
            'pull-ci-medik8s-self-node-remediation-main-5.0-e2e-system-tests-snr-pr-aws': 'SNR',
            'periodic-ci-openshift-rhwa-system-tests-main-5.0-tls-pqc-readiness-all': 'TLS',
        }
        for job_name, expected in cases.items():
            self.assertEqual(self.collector._extract_operator(job_name, ''), expected)

    def test_every_configured_job_resolves_step_and_operator(self):
        import yaml
        config = yaml.safe_load((ROOT / 'config.yaml').read_text())
        prow = config['collector']['prow_gcs']
        for job_name in prow['job_patterns'] + prow['presubmit_job_patterns']:
            with self.subTest(job_name=job_name):
                self.assertIsNotNone(self.collector._derive_step_name(job_name))
                self.assertIsNotNone(self.collector._extract_operator(job_name, ''))


if __name__ == '__main__':
    unittest.main()
