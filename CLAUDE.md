# CLAUDE.md - medik8s CI Dashboard

This file provides guidance to Claude Code when working with the CI dashboard codebase.

## Repository Overview

CI test results dashboard for medik8s RHWA operators (FAR, SBR, SNR, NHC, NMO, MDR). Collects Prow periodic and presubmit job results from GCS, stores in SQLite, serves a Flask web UI with charts and test analysis.

- **Upstream**: `medik8s/ci-dashboard` on GitHub
- **Language**: Python 3.10 (Flask + gunicorn)
- **Frontend**: Single-page HTML with vanilla JS and Chart.js (no build step)
- **Database**: SQLite (single file at `/data/dashboard.db` in container)
- **Default branch**: `master`

## Project Structure

```
config.yaml                    # Job patterns, versions, platforms, schedules
dashboard.py                   # CLI entrypoint (collect/serve commands)
wsgi.py                        # Gunicorn WSGI entrypoint
Dockerfile                     # Container image build
requirements.txt               # Python dependencies
src/
  collectors/
    prow_gcs.py                # Prow GCS bucket collector (primary)
    prow_mcp.py                # Prow MCP collector (alternative)
    reportportal.py            # ReportPortal collector (unused)
    base.py                    # Abstract collector interface
  storage/
    database.py                # SQLite schema, queries, CRUD
  web/
    server.py                  # Flask routes, API endpoints, FBC/Konflux integration
    templates/dashboard.html   # Single-page dashboard UI
    static/                    # Favicon, logo
  metrics/
    calculator.py              # Pass rate, trends, rankings
  reports/
    weekly_report.py           # Week-over-week comparison
  ai/
    analyzer.py                # AI failure analysis (Anthropic API)
  integrations/
    jira_integration.py        # Jira issue creation
    gangway_client.py          # Prow Gangway trigger API
openshift/
  deployment.yaml              # GPC deployment manifest
  cronjob.yaml                 # Daily data collection CronJob
  pvc.yaml                     # Persistent volume for SQLite
  route.yaml                   # OpenShift Route
  service.yaml                 # ClusterIP Service
```

## Common Development Commands

```bash
# Run locally
pip install -r requirements.txt
python dashboard.py serve --config config.yaml --db /tmp/dashboard.db

# Collect data locally
python dashboard.py collect --config config.yaml --db /tmp/dashboard.db --days 30

# Run with gunicorn (production-like)
python -m gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 wsgi:app
```

## Adding a New OCP Version

When a new OCP version needs tracking (e.g., adding 4.23 alongside existing 4.22):

1. **Update `config.yaml`**:
   - Add the version to `tracking.versions` list
   - Add new job patterns to `job_patterns` (periodic) and `presubmit_job_patterns` (presubmit)
   - Add entries to `job_schedules` if the new version has its own periodic schedule

2. **Example: adding 4.23 periodic jobs**:
   ```yaml
   tracking:
     versions:
       - "4.21"
       - "4.22"
       - "4.23"   # new

   # Under prow_gcs.job_patterns, add:
   - "periodic-ci-medik8s-system-tests-main-4.23-konflux-e2e-far-weekly-aws"
   - "periodic-ci-medik8s-system-tests-main-4.23-konflux-e2e-sbr-weekly-aws-odf"
   # ... repeat for each operator

   # Under prow_gcs.presubmit_job_patterns, add:
   - "pull-ci-medik8s-system-tests-main-4.23-konflux-e2e-far-aws"
   # ... repeat for each operator
   ```

3. **No code changes needed**: The collector, database, and UI are version-agnostic. The version filter dropdown populates from database content. Job name patterns in `config.yaml` are the only thing that determines what data gets collected.

4. **After updating config.yaml**: Rebuild and redeploy the container image, then trigger a data collection to populate the new version's data.

## Adding a New Operator

To track a new operator (e.g., a hypothetical new medik8s operator):

1. Add its periodic job pattern to `config.yaml` under `job_patterns`
2. Add its presubmit job pattern under `presubmit_job_patterns`
3. Add a schedule entry under `job_schedules`
4. The UI operator filter chips are hardcoded in `dashboard.html` - add a new `op-filter-chip` button with the operator's short name and color

## Adding a New Job Variant

To add a new test variant (e.g., disconnected, upgrade, different storage backend):

1. Add the full job name to `config.yaml` under `job_patterns`
2. Add a `job_schedules` entry with label, variant description, and schedule
3. The dashboard derives the operator name from the job name pattern (`e2e-<operator>` regex in `server.py`), so standard naming is important

## Key Configuration Concepts

- **`job_patterns`**: Full Prow periodic job names. Only jobs matching these patterns are collected.
- **`presubmit_job_patterns`**: Full Prow presubmit job names (triggered on PRs).
- **`job_schedules`**: Display metadata for the "Periodic Job Schedule" table in the UI. Keys should match the suffix after `e2e-` in the job name.
- **`tracking.versions`**: OCP versions to track. Used for version filter in the UI.
- **`tracking.lookback_days`**: How many days of history to collect (default: 90).

## API Endpoints

| Endpoint                      | Method | Description                                      |
| ----------------------------- | ------ | ------------------------------------------------ |
| `/`                           | GET    | Main dashboard HTML page                         |
| `/api/summary`                | GET    | Summary statistics (pass rate, counts)           |
| `/api/test-results`           | GET    | Enriched test results with all metadata          |
| `/api/job-runs`               | GET    | Job run history with failure categories          |
| `/api/fbc-summary`            | GET    | FBC validation: per-snapshot pass/fail           |
| `/api/operator-stats`         | GET    | Per-operator pass/fail counts                    |
| `/api/operator-health`        | GET    | Per-operator health: latest run + weekly history |
| `/api/trend`                  | GET    | Pass rate trend over time                        |
| `/api/presubmit-results`      | GET    | Presubmit E2E test results                       |
| `/api/trigger-job`            | POST   | Trigger a Prow job via Gangway                   |
| `/api/trigger-all-jobs`       | POST   | Trigger all periodic jobs via Gangway            |
| `/api/export`                 | GET    | Export test results to XLSX                      |
| `/api/export/operator-health` | GET    | Export Operator Health view to XLSX (3 sheets)   |
| `/api/jira/create`            | POST   | Create/find Jira issue for a failure             |
| `/api/analyze-failure`        | POST   | AI failure analysis                              |

Common query parameters: `days`, `version`, `operator`, `platform`.

## Code Style

- Python: Standard Python 3.10, no type-checking tool configured
- JavaScript: Vanilla JS in a single HTML file (no framework, no build step)
- HTML/CSS: Inline styles and `<style>` block in dashboard.html (dark theme, CSS variables)
- No linter or formatter configured

## GCS Backup Infrastructure

The SQLite database is backed up to GCS after each data collection cycle and restored on pod startup.

| Component         | Value                                                                      |
| ----------------- | -------------------------------------------------------------------------- |
| GCP Project       | `medik8s-qe-ci-dashboard`                                                  |
| Bucket            | `gs://medik8s-qe-ci-dashboard-backup`                                      |
| Blob path         | `backup/dashboard.db`                                                      |
| SA                | `ci-dashboard-vertex@medik8s-qe-ci-dashboard.iam.gserviceaccount.com`      |
| SA role on bucket | `roles/storage.objectAdmin`                                                |
| Versioning        | Enabled (every upload keeps previous version)                              |
| Lifecycle         | Non-current versions auto-deleted after 30 days                            |
| Env var           | `GCS_BACKUP_BUCKET` (set in `deployment.yaml`)                             |
| Auth              | `GOOGLE_APPLICATION_CREDENTIALS` pointing to the Vertex AI SA key (reused) |

To list backup versions:
```bash
gcloud storage ls -la gs://medik8s-qe-ci-dashboard-backup/backup/
```

To restore a specific older version:
```bash
gcloud storage cp "gs://medik8s-qe-ci-dashboard-backup/backup/dashboard.db#<GENERATION>" /tmp/restore.db
```

## PR Workflow

1. Create branch from `master`
2. Push to `upstream` remote (medik8s/ci-dashboard)
3. Create PR via `gh pr create --repo medik8s/ci-dashboard`
4. After merge, rebuild container image and redeploy
