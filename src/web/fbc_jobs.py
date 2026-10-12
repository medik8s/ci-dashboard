"""Configured FBC coverage and application-scoped snapshot identities."""

import re

FBC_JOB_RE = re.compile(
    r'^periodic-ci-openshift-rhwa-system-tests-main-'
    r'(?P<version>\d+\.\d+)-(?P<variant>konflux|disconnected|upgrade)-'
    r'e2e-(?P<name>[a-z0-9-]+)$'
)
FBC_SHA_LENGTH = 40
FBC_SHORT_SHA_LENGTH = 7


def expected_fbc_jobs(config):
    """Index configured FBC periodics by version without consulting run history."""
    schedules = config.get('fbc_schedule_overrides', {})
    jobs = {}
    for job_name in config.get('job_patterns', []):
        match = FBC_JOB_RE.fullmatch(job_name)
        if not match:
            continue
        version = match['version']
        jobs.setdefault(version, {})[job_name] = {
            'job_name': job_name,
            'display_name': match['name'],
            'operator': match['name'].split('-')[0].upper(),
            'schedule': schedules.get(job_name, {}).get('day', ''),
        }
    return jobs


def canonical_fbc_sha(tag, known_shas):
    """Expand only an unambiguous prefix from already-loaded application data."""
    tag = tag.lower()
    matches = {sha.lower() for sha in known_shas
               if len(sha) == FBC_SHA_LENGTH and sha.lower().startswith(tag)}
    if len(matches) == 1:
        return matches.pop()
    return tag
