# CI Failure Tracker Dashboard

Open source web dashboard for monitoring, analyzing, and tracking CI test failures in OpenShift. Features detailed failure metrics, test history, and export capabilities.

## Features

### Test Failure Tracking
- Real-time dashboard with test failure metrics
- Historical tracking (configurable time range)
- Platform-specific failure rates (AWS, Azure, GCP, vSphere, Nutanix)
- Version comparison across OpenShift releases
- Comprehensive test statistics and trends

### Interactive Test Analysis
- Click any test to view detailed failure information
- View error logs with syntax highlighting
- Filter failures by platform
- Timestamp and job information for each failure
- Direct links to Prow CI jobs

### Data Export
- Export test results in XLSX, CSV, or Markdown formats
- Version and time-range filtering
- Platform-specific sheets/sections
- Latest test run status (excludes skipped tests)
- Automated filename generation with metadata

## Quick Start

### Prerequisites
- Python 3.10+
- Access to OpenShift CI (for data collection)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/redhat-community-ai-tools/ci-dashboard-tracker.git
cd ci-dashboard-tracker
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure your jobs in `config.yaml`:
```yaml
collector:
  type: "reportportal"  # or "prow_gcs"
  
  reportportal:
    url: "https://your-reportportal-instance.com"
    project: "your-project"
    # API token from environment variable: REPORTPORTAL_API_TOKEN
    job_patterns:
      - "periodic-ci-openshift-COMPONENT-release-{version}-*"
```

4. Collect test data:
```bash
python dashboard.py collect --days 30
```

5. Start the web server:
```bash
python dashboard.py serve
```

6. Open http://localhost:8080 in your browser

## Configuration

Edit `config.yaml` to customize:

- **Job patterns**: Which CI jobs to monitor
- **Versions**: OpenShift versions to track
- **Platforms**: Cloud platforms to monitor
- **Time range**: How many days of history to collect
- **Blocklist**: Tests to exclude from dashboard

See `config.yaml` for detailed configuration options.

## Data Collectors

The dashboard supports multiple data collection methods:

- **reportportal**: ReportPortal API (requires authentication)
- **prow_gcs**: Direct GCS bucket access (requires credentials)

## AI-Powered Failure Analysis (Optional)

The dashboard includes AI-powered test failure analysis using Claude via Google Vertex AI. This feature provides root cause analysis, component identification, and suggested actions for test failures.

### Setup Vertex AI

1. **Enable Vertex AI API** in your Google Cloud project:
   ```bash
   gcloud services enable aiplatform.googleapis.com
   ```

2. **Set environment variables**:
   ```bash
   export ANTHROPIC_VERTEX_PROJECT_ID="your-gcp-project-id"
   export ANTHROPIC_VERTEX_REGION="global"  # or "us-east5" for regional
   ```

3. **Authenticate** with Google Cloud:
   ```bash
   gcloud auth application-default login
   ```

4. **Install the Anthropic SDK** with Vertex support:
   ```bash
   pip install 'anthropic[vertex]'
   ```

### Using AI Analysis

Once configured, click the "AI Analyze" button on any failed test in the dashboard to get:
- Root cause analysis
- Affected component identification
- Classification (product bug, automation issue, infrastructure problem, etc.)
- Platform-specific failure patterns
- Suggested remediation actions
- JIRA-ready issue descriptions

**Cost**: Approximately $0.02 per test analysis using Claude Sonnet.

**Note**: The dashboard only supports Vertex AI for AI analysis. Direct Anthropic API access is not configured.

## Deployment

### Local Development
```bash
python dashboard.py serve --port 8080
```

### OpenShift Deployment

The pre-built container image is available at [quay.io/medik8s-qe/ci-dashboard](https://quay.io/repository/medik8s-qe/ci-dashboard).

See `openshift/` directory for deployment manifests.

Basic deployment:
```bash
oc apply -f openshift/
```

## Usage

### Collect Data
```bash
# Collect last 30 days
python dashboard.py collect --days 30

# Collect specific version
python dashboard.py collect --days 7 --version 4.22

# Collect specific platform
python dashboard.py collect --days 14 --platform aws
```

### Export Data
Access exports via the web UI or API:
```bash
curl "http://localhost:8080/api/export?format=xlsx&days=7&version=4.22" -o export.xlsx
```

## Customization for Your Team

This dashboard is designed to be customized for any OpenShift testing team:

1. Update `config.yaml` with your job patterns
2. Modify `tracking.test_suite_filter` to match your test suite
3. Add your blocklist of tests to exclude
4. Customize platforms and versions

Example for Storage team:
```yaml
collector:
  reportportal:
    job_patterns:
      - "periodic-ci-*-storage-*"
      - "periodic-ci-*-csi-*"

tracking:
  test_suite_filter: "Storage"
  platforms:
    - "aws"
    - "azure"
    - "gcp"
```

## Architecture

- **Data Storage**: SQLite database
- **Web Framework**: Flask
- **Frontend**: Vanilla JavaScript with modern CSS
- **Data Collection**: Pluggable collectors (ReportPortal, Prow GCS)
- **Export**: OpenPyXL for Excel, CSV, Markdown

## Contributing

Contributions welcome! This is an open source project maintained by the OpenShift QE community.

## License

Apache 2.0

## Support

For issues and questions, please open a GitHub issue.
