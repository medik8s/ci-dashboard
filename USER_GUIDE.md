# medik8s Test Dashboard - User Guide

Simple guide for viewing medik8s test health and pass rates.

## What This Dashboard Shows

- **Test Pass Rates**: How many tests are passing vs failing
- **Trends Over Time**: Is test health improving or declining?
- **Version Comparison**: How 4.21 compares to 4.22
- **Problem Tests**: Which tests are failing most often

## Prerequisites

- Python 3.10 or higher
- Network access to Prow GCS bucket (public, no auth needed)

## One-Time Setup

### 1. Install the Dashboard

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Daily Usage

### Step 1: Update Data (Do This Once Per Day)

```bash
source venv/bin/activate
./dashboard.py collect --days 90
```

**What this does:** Downloads the last 90 days of test results from Prow GCS (public bucket, no VPN needed)
**How long it takes:** About 30 seconds

### Step 2: Start the Dashboard

```bash
./dashboard.py serve
```

**What this does:** Starts a local web server
**Where to open:** http://localhost:8080

Press **Ctrl+C** when you're done to stop the server.

## Using the Dashboard

### Understanding the Dashboard

When you open http://localhost:8080, you'll see:

#### Top Section - Summary Cards
- **Average Pass Rate**: Overall percentage of passing tests
- **Total Runs**: Number of test runs in the selected period
- **Trend**: Whether things are improving, declining, or stable
- **TLS Pass Rate**: Counts only TLS scanner assertions, excluding cluster installation and cleanup checks. The overview Operator Health table shows "No scanner results" when no non-skipped scanner assertions are available. This does not prove the scanner never ran. TLS job chips and last-run dates in the overview and Weekly Schedule reflect the recorded job executions, including setup failures.

#### Middle Section - Charts
- **Pass Rate Trend Over Time**: Line graph showing daily pass rates
- **Pass Rate by Version**: Bar chart comparing 4.21 vs 4.22

#### Bottom Section - Test Rankings
- **Lowest Performing Tests**: Table showing which tests are failing most
- Tests are sorted worst-first (0% at top, 100% at bottom)
- Shows test description, version, number of runs, and pass rate

### Filters

#### Time Range (Top Left)
- **Last 7 days**: Recent trend
- **Last 14 days**: Two-week view
- **Last 30 days**: Monthly overview (recommended)
- **Last 60 days**: Longer trend
- **Last 90 days**: Quarterly view

#### Version Filter (Top Right)
- **All Versions**: Combined data from 4.21 and 4.22
- **4.21**: Only show 4.21 test results
- **4.22**: Only show 4.22 test results

### Common Questions

**Q: How often should I update the data?**
A: Once per day is sufficient. Run `./dashboard.py collect --days 90` each morning.

**Q: What's a good pass rate?**
A: For medik8s tests:
- Above 90%: Excellent
- 80-90%: Good
- 70-80%: Needs attention
- Below 70%: Critical, investigate immediately

**Q: Why are some tests at 0%?**
A: They failed every single time in the selected period. These are top priority to fix.

**Q: The numbers look wrong. What should I do?**
A:
1. Verify `config.yaml` job names match current Prow periodic jobs
2. Run `./dashboard.py collect --days 90` to refresh data
3. Restart the dashboard with `./dashboard.py serve`

**Q: Can I share this dashboard with others?**
A: No, it's running on your local machine (localhost). Each person needs to run their own instance, or we need to deploy it to a shared server.

## Quick Command Reference

```bash
# 1. Start virtual environment (always do this first)
source venv/bin/activate

# 2. Collect latest data (once per day)
./dashboard.py collect --days 90

# 3. Start dashboard
./dashboard.py serve

# 4. Quick stats without opening web browser
./dashboard.py stats --days 7

# 5. Stop dashboard
# Press Ctrl+C
```

## Troubleshooting

### Error: "Connection failed"
**Solution:** Check network connectivity. Prow GCS is public, but the OpenShift-deployed dashboard requires VPN (route uses `shard: internal`).

### Error: "No job runs found"
**Solution:** Verify that `config.yaml` has the correct Prow job names and that `lookback_days` is large enough for weekly jobs

### Error: "Database not found"
**Solution:** Run `./dashboard.py collect --days 90` first to create the database

### Dashboard shows old data
**Solution:** Run `./dashboard.py collect --days 90` to refresh

### Dashboard won't start
**Solution:**
1. Make sure you ran `source venv/bin/activate` first
2. Try running: `pip install -r requirements.txt`
3. Check if another dashboard is already running: `pkill -f "dashboard.py serve"`

## Tips for Managers

### Weekly Review Workflow

1. **Monday morning:**
   ```bash
   source venv/bin/activate
   ./dashboard.py collect --days 90
   ./dashboard.py serve
   ```

2. **Open dashboard:** http://localhost:8080

3. **Check these metrics:**
   - Overall pass rate (should be >85%)
   - Version comparison (is 4.22 worse than 4.21?)
   - Trend arrow (improving vs declining)
   - Top 5 worst tests (what needs immediate attention?)

4. **Take action:**
   - Tests below 50%: File Jira tickets
   - Declining trend: Discuss with team
   - Version differences: Investigate regression

### Monthly Reporting

Use the dashboard to generate monthly test health reports:

1. Set time range to "Last 30 days"
2. Screenshot the summary cards and charts
3. Export test rankings table (copy/paste into email)
4. Include in status report to leadership

### Comparing Sprint Results

To see if last sprint improved test health:

1. **Before sprint:** Note the overall pass rate
2. **After sprint:** Run collect and check new pass rate
3. **Compare:** Did we go from 82% to 88%? Success!

## Configuration

The file `config.yaml` controls what data is collected:

### Add/Remove Versions

```yaml
tracking:
  versions:
    - "4.21"
    - "4.22"
    - "4.23"  # Add new version
```

### Exclude Broken/Removed Tests

```yaml
tracking:
  blocklist:
    - "Verify FenceAgentsRemediation CR remediation flow"
    - "Some flaky test description to exclude"
```

Blocklist entries match the `test_name` stored in the database. For medik8s Ginkgo tests, this is the cleaned test description (not an OCP-XXXXX ID). These tests will be hidden from the dashboard even if they appear in Prow GCS results.

## Support

For technical issues or questions:
- **Contact:** Ronnie Rasouli (rrasouli@redhat.com)
- **Team:** medik8s QE Team

---

**Last Updated:** March 23, 2026
**Dashboard Version:** 1.0
