# OpenShift Deployment

Deploy CI Dashboard to OpenShift (GPC).

## Prerequisites

1. Access to OpenShift cluster (GPC)
2. `oc` CLI tool installed and logged in
3. VPN or internal network access (Route uses `shard: internal`)

## Quick Deploy

```bash
# 1. Create project
oc new-project ci-dashboard \
  --display-name="CI Dashboard" \
  --description="medik8s QE CI test health tracking"

# 2. Deploy all resources
oc apply -f openshift/

# 3. Get the dashboard URL
oc get route ci-dashboard -o jsonpath='{.spec.host}'
```

## Manual Steps

### 1. Deploy Resources

```bash
# Persistent storage
oc apply -f pvc.yaml

# Web application
oc apply -f deployment.yaml
oc apply -f service.yaml
oc apply -f route.yaml
```

### 2. Verify Deployment

```bash
# Check pod status
oc get pods

# Check logs
oc logs -f deployment/ci-dashboard

# Get public URL
oc get route ci-dashboard
```

## Data Collection

The dashboard uses **automatic scheduled collection** plus **manual on-demand refresh**:

### Automatic Collection (CronJob)

A Kubernetes CronJob (`dashboard-collect`) triggers data collection every 4
hours by sending an HTTP POST to the Flask app's
`/api/trigger-collection` endpoint. The app then collects the latest test
results from Prow GCS in a background thread. This keeps presubmit and
periodic job data fresh without manual intervention.

### Manual Refresh

Click the **"Refresh Data"** button in the dashboard header to trigger an
immediate data refresh between scheduled collections.

**Collection process:**
1. Fetches latest test results from Prow GCS (last 90 days)
2. Parses JUnit XML results from each job run
3. Stores test results in SQLite database
4. Progress shown in blue banner with real-time status
5. Dashboard data refreshes automatically when complete

**What's collected:**
- Periodic job runs (medik8s weekly jobs)
- Test results (pass/fail/skip with error messages)
- Pass rates, timestamps, versions, platforms

### View Test Details

Each failing test shows the JUnit failure message and stack trace.
For full CI logs, navigate to the Prow job URL linked in the test details.

## Troubleshooting

### Pod not starting

```bash
# Check events
oc get events --sort-by='.lastTimestamp'

# Check pod logs
oc logs deployment/ci-dashboard

# Describe pod
oc describe pod -l app=ci-dashboard
```

### Database issues

If the dashboard shows "No data available", data collection may still
be in progress:

```bash
# Check collection status via API
curl https://$(oc get route ci-dashboard \
  -o jsonpath='{.spec.host}')/api/collection-status

# View pod logs to see collection progress
oc logs -f deployment/ci-dashboard

# Check database contents (python:3.10-slim does not include sqlite3 CLI)
oc rsh deployment/ci-dashboard \
  python3 -c "import sqlite3; c=sqlite3.connect('/data/dashboard.db'); print('Job runs:', c.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0])"
```

### Update deployment

```bash
# Apply changes
oc apply -f deployment.yaml

# Force rollout
oc rollout restart deployment/ci-dashboard
```

## Resources

- **Memory**: 256Mi request, 512Mi limit
- **CPU**: 100m request, 500m limit
- **Storage**: 1Gi persistent volume

## Architecture

```
Internet                CronJob (every 4h)
   |                       |
Route (HTTPS, internal)    | HTTP POST
   |                       | /api/trigger-collection
   '--> Service (8080) <---'
           |
   Deployment (gunicorn + Flask)
      |-- Background data collection thread
      '-- PersistentVolumeClaim (SQLite DB)
```

**Data Flow:**
1. User accesses dashboard via HTTPS route
2. CronJob triggers collection via HTTP POST every 4 hours (or user clicks "Refresh Data")
3. Flask background thread collects data from Prow GCS
4. Results stored in SQLite database on persistent volume
5. Dashboard displays analytics and reports
