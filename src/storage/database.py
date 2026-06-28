"""
SQLite database for storing historical test results and metrics
"""

import logging
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

from collectors.base import JobRun, TestResult, TestStatus

logger = logging.getLogger(__name__)


class DashboardDatabase:
    """SQLite database for historical test data"""

    def __init__(self, db_path: str):
        """
        Initialize database connection

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._check_integrity_and_recover(db_path)

        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row

        # Use DELETE journal mode (not WAL) for NFS compatibility.
        # WAL requires shared-memory mmap which NFS does not support reliably,
        # causing "database disk image is malformed" corruption.
        mode = self.conn.execute('PRAGMA journal_mode=DELETE').fetchone()
        current_mode = mode[0].lower() if mode and mode[0] else 'unknown'
        if current_mode != 'delete':
            self.conn.close()
            if current_mode == 'wal':
                raise RuntimeError(
                    "SQLite remained in WAL mode, which is unsafe on NFS storage. "
                    "Another process may be holding the database open."
                )
            raise RuntimeError(
                f"SQLite must use DELETE journal mode for NFS safety, "
                f"but got: {current_mode}"
            )
        self.conn.execute('PRAGMA synchronous=FULL')

        self._create_tables()

    @staticmethod
    def _check_integrity_and_recover(db_path: str):
        """Check database integrity on startup; delete and recreate if corrupt."""
        if not Path(db_path).exists():
            return

        def _purge_db_files():
            for suffix in ('', '-wal', '-shm', '-journal'):
                try:
                    Path(f"{db_path}{suffix}").unlink(missing_ok=True)
                except OSError as err:
                    logger.warning("Could not delete %s%s: %s", db_path, suffix, err)

        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                jmode = conn.execute('PRAGMA journal_mode=DELETE').fetchone()
                if not jmode or jmode[0].lower() != 'delete':
                    logger.warning(
                        "Integrity-check connection could not switch to DELETE mode "
                        "(got: %s); proceeding anyway",
                        jmode[0] if jmode else 'unknown',
                    )
                result = conn.execute('PRAGMA integrity_check').fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if 'locked' in msg or 'busy' in msg:
                logger.warning("Database locked during integrity check, skipping: %s", e)
                return
            if any(term in msg for term in ('malformed', 'corrupt', 'not a database')):
                logger.warning("Corrupt database detected (%s), deleting and recreating", e)
                _purge_db_files()
                return
            logger.warning("Database unavailable during integrity check: %s", e)
            return
        except sqlite3.DatabaseError as e:
            msg = str(e).lower()
            if any(term in msg for term in ('malformed', 'corrupt', 'not a database')):
                logger.warning("Corrupt database detected (%s), deleting and recreating", e)
                _purge_db_files()
                return
            logger.warning(
                "Database error during integrity check, but corruption not confirmed; "
                "leaving database in place: %s", e
            )
            return

        if not result or result[0] != 'ok':
            logger.warning(
                "Corrupt database detected (integrity_check: %s), deleting and recreating",
                result[0] if result else "no result",
            )
            _purge_db_files()

    def _create_tables(self):
        """Create database schema"""

        cursor = self.conn.cursor()

        # Job runs table - stores overall job statistics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                build_id TEXT NOT NULL,
                status TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                duration_seconds REAL,
                version TEXT NOT NULL,
                platform TEXT NOT NULL,
                total_tests INTEGER NOT NULL,
                passed_tests INTEGER NOT NULL,
                failed_tests INTEGER NOT NULL,
                skipped_tests INTEGER NOT NULL,
                pass_rate REAL NOT NULL,
                job_url TEXT,
                ocp_version TEXT,
                csv_version TEXT,
                fbc_image TEXT,
                step_name TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_name, build_id)
            )
        """)

        # Test results table - stores individual test results
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_name TEXT NOT NULL,
                test_description TEXT,
                status TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                duration_seconds REAL,
                error_message TEXT,
                job_name TEXT NOT NULL,
                build_id TEXT NOT NULL,
                version TEXT NOT NULL,
                platform TEXT NOT NULL,
                job_url TEXT,
                log_url TEXT,
                manual_classification TEXT,
                classified_by TEXT,
                classification_timestamp DATETIME,
                jira_issue_key TEXT,
                polarion_id TEXT,
                operator TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(test_name, job_name, build_id)
            )
        """)

        # Daily metrics table - pre-aggregated daily statistics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                version TEXT NOT NULL,
                platform TEXT,
                total_runs INTEGER NOT NULL,
                passed_runs INTEGER NOT NULL,
                failed_runs INTEGER NOT NULL,
                total_tests INTEGER NOT NULL,
                passed_tests INTEGER NOT NULL,
                failed_tests INTEGER NOT NULL,
                overall_pass_rate REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, version, platform)
            )
        """)

        # Test metrics table - per-test aggregated statistics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_name TEXT NOT NULL,
                date DATE NOT NULL,
                version TEXT NOT NULL,
                platform TEXT,
                total_runs INTEGER NOT NULL,
                passed_runs INTEGER NOT NULL,
                failed_runs INTEGER NOT NULL,
                pass_rate REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(test_name, date, version, platform)
            )
        """)

        # AI analyses table - stores AI-generated failure analyses
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_name TEXT NOT NULL,
                version TEXT NOT NULL,
                platform TEXT,
                analysis_date DATETIME NOT NULL,
                root_cause TEXT,
                component TEXT,
                confidence INTEGER,
                failure_type TEXT,
                platform_specific INTEGER,
                affected_platforms TEXT,
                evidence TEXT,
                suggested_action TEXT,
                issue_title TEXT,
                issue_description TEXT,
                analysis_mode TEXT,
                cost REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(test_name, version, analysis_date)
            )
        """)

        # Create indexes for faster queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_timestamp ON job_runs(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_version ON job_runs(version)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_results_timestamp ON test_results(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_results_test_name ON test_results(test_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_metrics_test_name ON test_metrics(test_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_analyses_test_name ON ai_analyses(test_name)")

        # Add manual_classification column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE test_results ADD COLUMN manual_classification TEXT")
            cursor.execute("ALTER TABLE test_results ADD COLUMN classified_by TEXT")
            cursor.execute("ALTER TABLE test_results ADD COLUMN classification_timestamp DATETIME")
        except sqlite3.OperationalError:
            # Column already exists
            pass

        # Add jira_issue_key column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE test_results ADD COLUMN jira_issue_key TEXT")
        except sqlite3.OperationalError:
            # Column already exists
            pass

        # Add Polarion ID and operator columns
        existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(test_results)")}
        if 'polarion_id' not in existing_cols:
            cursor.execute("ALTER TABLE test_results ADD COLUMN polarion_id TEXT")
        if 'operator' not in existing_cols:
            cursor.execute("ALTER TABLE test_results ADD COLUMN operator TEXT")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_results_operator ON test_results(operator)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_results_polarion_id ON test_results(polarion_id)")

        # Add enriched metadata columns to job_runs
        jr_cols = {row[1] for row in cursor.execute("PRAGMA table_info(job_runs)")}
        for col in ['ocp_version', 'csv_version', 'fbc_image', 'step_name',
                     'failure_reason', 'failed_step', 'failure_category']:
            if col not in jr_cols:
                cursor.execute(f"ALTER TABLE job_runs ADD COLUMN {col} TEXT")

        # Add presubmit support columns to job_runs
        if 'job_type' not in jr_cols:
            cursor.execute("ALTER TABLE job_runs ADD COLUMN job_type TEXT DEFAULT 'periodic'")
        if 'pr_number' not in jr_cols:
            cursor.execute("ALTER TABLE job_runs ADD COLUMN pr_number INTEGER")
        if 'pr_author' not in jr_cols:
            cursor.execute("ALTER TABLE job_runs ADD COLUMN pr_author TEXT")
        if 'pr_repo' not in jr_cols:
            cursor.execute("ALTER TABLE job_runs ADD COLUMN pr_repo TEXT")
        if 'gcs_prefix' not in jr_cols:
            cursor.execute("ALTER TABLE job_runs ADD COLUMN gcs_prefix TEXT")

        # Add presubmit support columns to test_results
        tr_cols = {row[1] for row in cursor.execute("PRAGMA table_info(test_results)")}
        if 'job_type' not in tr_cols:
            cursor.execute("ALTER TABLE test_results ADD COLUMN job_type TEXT DEFAULT 'periodic'")
        if 'pr_number' not in tr_cols:
            cursor.execute("ALTER TABLE test_results ADD COLUMN pr_number INTEGER")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_job_type ON job_runs(job_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_test_results_job_type ON test_results(job_type)")

        self.conn.commit()

    def insert_job_runs(self, job_runs: List[JobRun]) -> int:
        """
        Insert job runs into database

        Args:
            job_runs: List of JobRun objects

        Returns:
            Number of rows inserted
        """
        cursor = self.conn.cursor()
        inserted = 0

        for run in job_runs:
            try:
                cursor.execute("""
                    INSERT INTO job_runs (
                        job_name, build_id, status, timestamp, duration_seconds,
                        version, platform, total_tests, passed_tests, failed_tests,
                        skipped_tests, pass_rate, job_url,
                        ocp_version, csv_version, fbc_image, step_name,
                        job_type, pr_number, pr_author, pr_repo, gcs_prefix,
                        failure_reason, failed_step, failure_category
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_name, build_id) DO UPDATE SET
                        status = excluded.status,
                        timestamp = excluded.timestamp,
                        duration_seconds = excluded.duration_seconds,
                        version = excluded.version,
                        platform = excluded.platform,
                        total_tests = excluded.total_tests,
                        passed_tests = excluded.passed_tests,
                        failed_tests = excluded.failed_tests,
                        skipped_tests = excluded.skipped_tests,
                        pass_rate = excluded.pass_rate,
                        job_url = excluded.job_url,
                        ocp_version = COALESCE(excluded.ocp_version, job_runs.ocp_version),
                        csv_version = COALESCE(excluded.csv_version, job_runs.csv_version),
                        fbc_image = COALESCE(excluded.fbc_image, job_runs.fbc_image),
                        step_name = COALESCE(excluded.step_name, job_runs.step_name),
                        job_type = excluded.job_type,
                        pr_number = excluded.pr_number,
                        pr_author = COALESCE(excluded.pr_author, job_runs.pr_author),
                        pr_repo = COALESCE(excluded.pr_repo, job_runs.pr_repo),
                        gcs_prefix = COALESCE(excluded.gcs_prefix, job_runs.gcs_prefix),
                        failure_reason = COALESCE(excluded.failure_reason, job_runs.failure_reason),
                        failed_step = COALESCE(excluded.failed_step, job_runs.failed_step),
                        failure_category = COALESCE(excluded.failure_category, job_runs.failure_category)
                """, (
                    run.job_name,
                    run.build_id,
                    run.status.value,
                    run.timestamp.isoformat(),
                    run.duration_seconds,
                    run.version,
                    run.platform,
                    run.total_tests,
                    run.passed_tests,
                    run.failed_tests,
                    run.skipped_tests,
                    run.pass_rate,
                    run.job_url,
                    run.ocp_version,
                    run.csv_version,
                    run.fbc_image,
                    run.step_name,
                    getattr(run, 'job_type', 'periodic'),
                    getattr(run, 'pr_number', None),
                    getattr(run, 'pr_author', None),
                    getattr(run, 'pr_repo', None),
                    getattr(run, 'gcs_prefix', None),
                    getattr(run, 'failure_reason', None),
                    getattr(run, 'failed_step', None),
                    getattr(run, 'failure_category', None),
                ))
                inserted += 1
            except sqlite3.IntegrityError:
                # Already exists, skip
                pass

        self.conn.commit()
        return inserted

    def insert_test_results(self, test_results: List[TestResult]) -> int:
        """
        Insert test results into database

        Args:
            test_results: List of TestResult objects

        Returns:
            Number of rows inserted
        """
        cursor = self.conn.cursor()
        inserted = 0

        for result in test_results:
            try:
                cursor.execute("""
                    INSERT INTO test_results (
                        test_name, test_description, status, timestamp, duration_seconds, error_message,
                        job_name, build_id, version, platform, job_url, log_url,
                        polarion_id, operator, job_type, pr_number
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(test_name, job_name, build_id) DO UPDATE SET
                        test_description = excluded.test_description,
                        status = excluded.status,
                        timestamp = excluded.timestamp,
                        duration_seconds = excluded.duration_seconds,
                        error_message = excluded.error_message,
                        version = excluded.version,
                        platform = excluded.platform,
                        job_url = excluded.job_url,
                        log_url = excluded.log_url,
                        polarion_id = COALESCE(excluded.polarion_id, test_results.polarion_id),
                        operator = COALESCE(excluded.operator, test_results.operator),
                        job_type = excluded.job_type,
                        pr_number = excluded.pr_number
                """, (
                    result.test_name,
                    result.test_description,
                    result.status.value,
                    result.timestamp.isoformat(),
                    result.duration_seconds,
                    result.error_message,
                    result.job_name,
                    result.build_id,
                    result.version,
                    result.platform,
                    result.job_url,
                    result.log_url,
                    result.polarion_id,
                    result.operator,
                    getattr(result, 'job_type', 'periodic'),
                    getattr(result, 'pr_number', None),
                ))
                inserted += 1
            except sqlite3.IntegrityError:
                # Already exists, skip
                pass

        self.conn.commit()
        return inserted

    def get_daily_pass_rates(
        self,
        start_date: datetime,
        end_date: datetime,
        version: Optional[str] = None,
        platform: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get daily pass rates within date range

        Args:
            start_date: Start date
            end_date: End date
            version: Optional version filter
            platform: Optional platform filter

        Returns:
            List of daily metrics dictionaries
        """
        cursor = self.conn.cursor()

        cte = """
            WITH tr_agg AS (
                SELECT DATE(timestamp) as date, version, platform,
                       CAST(SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS REAL)
                            / NULLIF(COUNT(*), 0) * 100 as pass_rate
                FROM test_results
                WHERE status != 'skipped'
                AND COALESCE(job_type, 'periodic') = 'periodic'
                AND timestamp >= ? AND timestamp <= ?
        """
        cte_params = [start_date.isoformat(), end_date.isoformat()]

        if version:
            cte += " AND version = ?"
            cte_params.append(version)
        if platform:
            cte += " AND platform = ?"
            cte_params.append(platform)

        cte += " GROUP BY DATE(timestamp), version, platform)"

        main = """
            SELECT
                DATE(jr.timestamp) as date,
                jr.version,
                jr.platform,
                SUM(CASE WHEN jr.total_tests > 0 THEN 1 ELSE 0 END) as total_runs,
                -- Primary: test-weighted from job_runs; fallback: per-row from test_results
                COALESCE(
                    CASE WHEN SUM(jr.total_tests) > 0
                         THEN CAST(SUM(jr.passed_tests) AS REAL) / SUM(jr.total_tests) * 100
                    END,
                    MAX(tr_agg.pass_rate)
                ) as avg_pass_rate
            FROM job_runs jr
            -- tr_agg: 1 row per (date, version, platform). Multiple job_runs per group are collapsed by GROUP BY.
            LEFT JOIN tr_agg ON DATE(jr.timestamp) = tr_agg.date
                AND jr.version = tr_agg.version
                AND jr.platform = tr_agg.platform
            WHERE jr.timestamp >= ? AND jr.timestamp <= ?
            AND COALESCE(jr.job_type, 'periodic') = 'periodic'
        """
        main_params = [start_date.isoformat(), end_date.isoformat()]

        if version:
            main += " AND jr.version = ?"
            main_params.append(version)
        if platform:
            main += " AND jr.platform = ?"
            main_params.append(platform)

        main += """ GROUP BY DATE(jr.timestamp), jr.version, jr.platform
            HAVING SUM(CASE WHEN jr.total_tests > 0 THEN 1 ELSE 0 END) > 0
                OR MAX(tr_agg.pass_rate) IS NOT NULL
            ORDER BY date"""

        cursor.execute(cte + main, cte_params + main_params)
        return [dict(row) for row in cursor.fetchall()]

    def get_test_pass_rates(
        self,
        start_date: datetime,
        end_date: datetime,
        test_name: Optional[str] = None,
        version: Optional[str] = None,
        platform: Optional[str] = None,
        blocklist: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get per-test pass rates

        Args:
            start_date: Start date
            end_date: End date
            test_name: Optional test name filter
            version: Optional version filter
            platform: Optional platform filter
            blocklist: Optional list of test names to exclude

        Returns:
            List of test metrics dictionaries
        """
        cursor = self.conn.cursor()

        query = """
            SELECT
                test_name,
                MAX(test_description) as test_description,
                version,
                COUNT(*) as total_runs,
                SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) as passed_runs,
                CAST(SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) * 100 as pass_rate,
                GROUP_CONCAT(DISTINCT CASE WHEN status = 'failed' THEN platform END) as failed_platforms,
                (SELECT error_message FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.status = 'failed'
                 AND tr2.error_message IS NOT NULL
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as sample_error,
                (SELECT platform FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.status = 'failed'
                 AND tr2.error_message IS NOT NULL
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as sample_error_platform,
                (SELECT timestamp FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.status = 'failed'
                 AND tr2.error_message IS NOT NULL
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as sample_error_timestamp,
                (SELECT job_name FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.status = 'failed'
                 AND tr2.error_message IS NOT NULL
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as sample_error_job_name,
                (SELECT build_id FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.status = 'failed'
                 AND tr2.error_message IS NOT NULL
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as sample_error_build_id,
                (SELECT job_url FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.status = 'failed'
                 AND tr2.error_message IS NOT NULL
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as sample_error_job_url,
                (SELECT timestamp FROM test_results tr2
                 WHERE tr2.test_name = test_results.test_name
                 AND tr2.version = test_results.version
                 AND tr2.timestamp >= ?
                 AND tr2.timestamp <= ?
                 ORDER BY tr2.timestamp DESC
                 LIMIT 1) as last_run_timestamp
            FROM test_results
            WHERE timestamp >= ? AND timestamp <= ?
            AND status != 'skipped'
            AND COALESCE(job_type, 'periodic') = 'periodic'
        """

        params = [start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat(),
                  start_date.isoformat(), end_date.isoformat()]

        if test_name:
            query += " AND test_name = ?"
            params.append(test_name)

        if version:
            query += " AND version = ?"
            params.append(version)

        if platform:
            query += " AND platform = ?"
            params.append(platform)

        if blocklist:
            # Use LIKE to match test ID prefix (e.g., OCP-60944 matches OCP-60944:author:...)
            blocklist_conditions = ' AND '.join([f"test_name NOT LIKE ?" for _ in blocklist])
            query += f" AND ({blocklist_conditions})"
            params.extend([f"{test_id}%" for test_id in blocklist])

        query += " GROUP BY test_name, version ORDER BY pass_rate ASC"

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_version_comparison(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Compare pass rates across versions

        Args:
            start_date: Start date
            end_date: End date

        Returns:
            List of version comparison dictionaries
        """
        cursor = self.conn.cursor()

        query = """
            SELECT
                version,
                COUNT(*) as total_runs,
                SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) as passed_runs,
                CAST(SUM(passed_tests) AS REAL) / SUM(total_tests) * 100 as avg_pass_rate,
                AVG(total_tests) as avg_total_tests
            FROM job_runs
            WHERE timestamp >= ? AND timestamp <= ?
            AND total_tests >= 1
            AND COALESCE(job_type, 'periodic') = 'periodic'
            GROUP BY version
            ORDER BY version
        """

        cursor.execute(query, [start_date.isoformat(), end_date.isoformat()])
        return [dict(row) for row in cursor.fetchall()]

    def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """
        Execute a raw SQL query and return results

        Args:
            query: SQL query string
            params: Query parameters tuple

        Returns:
            List of result rows as dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def save_ai_analysis(
        self,
        test_name: str,
        version: str,
        platform: str = None,
        analysis: Dict[str, Any] = None
    ) -> int:
        """
        Save AI analysis to database (shared across all platforms)

        Args:
            test_name: Test name
            version: OpenShift version
            platform: Platform name (ignored - analysis is shared across platforms)
            analysis: Analysis dictionary with keys like root_cause, component, etc.

        Returns:
            Number of rows inserted (1 if successful)
        """
        cursor = self.conn.cursor()

        try:
            cursor.execute("""
                INSERT OR REPLACE INTO ai_analyses (
                    test_name, version, platform, analysis_date,
                    root_cause, component, confidence, failure_type,
                    platform_specific, affected_platforms, evidence,
                    suggested_action, issue_title, issue_description,
                    analysis_mode, cost
                ) VALUES (?, ?, NULL, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                test_name,
                version,
                analysis.get('root_cause'),
                analysis.get('component'),
                analysis.get('confidence', 0),
                analysis.get('failure_type'),
                1 if analysis.get('platform_specific') else 0,
                ','.join(analysis.get('affected_platforms', [])),
                analysis.get('evidence'),
                analysis.get('suggested_action'),
                analysis.get('issue_title'),
                analysis.get('issue_description'),
                analysis.get('analysis_mode'),
                analysis.get('cost', 0.0)
            ))

            self.conn.commit()
            return 1

        except sqlite3.IntegrityError:
            return 0

    def get_ai_analysis(
        self,
        test_name: str,
        version: str,
        platform: str = None,
        days: int = 30
    ) -> Optional[Dict[str, Any]]:
        """
        Get most recent AI analysis for a test (shared across all platforms)

        Args:
            test_name: Test name
            version: OpenShift version
            platform: Platform name (ignored - analysis is shared across platforms)
            days: How many days back to look

        Returns:
            Analysis dictionary or None if not found
        """
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT * FROM ai_analyses
            WHERE test_name = ?
            AND version = ?
            AND analysis_date >= datetime('now', ? || ' days')
            ORDER BY analysis_date DESC
            LIMIT 1
        """, (test_name, version, f'-{days}'))

        row = cursor.fetchone()
        if row:
            analysis = dict(row)
            # Convert affected_platforms back to list
            if analysis.get('affected_platforms'):
                analysis['affected_platforms'] = analysis['affected_platforms'].split(',')
            # Convert platform_specific back to boolean
            analysis['platform_specific'] = bool(analysis.get('platform_specific'))
            return analysis
        return None

    def save_manual_classification(
        self,
        test_name: str,
        version: str,
        platform: str = None,
        classification: str = None,
        classified_by: str = 'user'
    ) -> int:
        """
        Save manual classification for a test failure (applies to ALL platforms)

        Args:
            test_name: Test name
            version: OpenShift version
            platform: Platform name (ignored - classification applies to all platforms)
            classification: Classification (product_bug, automation_bug, system_issue, transient, to_investigate)
            classified_by: Who classified it (default: 'user')

        Returns:
            Number of rows updated
        """
        cursor = self.conn.cursor()

        try:
            cursor.execute("""
                UPDATE test_results
                SET manual_classification = ?,
                    classified_by = ?,
                    classification_timestamp = datetime('now')
                WHERE test_name = ?
                AND version = ?
                AND status = 'failed'
            """, (classification, classified_by, test_name, version))

            self.conn.commit()
            return cursor.rowcount

        except Exception as e:
            print(f"Error saving manual classification: {e}")
            return 0

    def save_jira_issue(
        self,
        test_name: str,
        version: str,
        platform: str = None,
        jira_issue_key: str = None
    ) -> int:
        """
        Save Jira issue key for a test failure (applies to ALL platforms)

        Args:
            test_name: Test name
            version: OpenShift version
            platform: Platform name (ignored - Jira issue applies to all platforms)
            jira_issue_key: Jira issue key (e.g., RHWA-1107)

        Returns:
            Number of rows updated
        """
        cursor = self.conn.cursor()

        try:
            # Log for debugging
            print(f"Saving Jira issue: {jira_issue_key} for test={test_name}, version={version} (all platforms)")

            cursor.execute("""
                UPDATE test_results
                SET jira_issue_key = ?
                WHERE test_name = ?
                AND version = ?
                AND status = 'failed'
            """, (jira_issue_key, test_name, version))

            self.conn.commit()
            rows_updated = cursor.rowcount
            print(f"Updated {rows_updated} rows with Jira issue key {jira_issue_key}")
            return rows_updated

        except Exception as e:
            print(f"Error saving Jira issue key: {e}")
            return 0

    def get_analysis_stats(self) -> Dict[str, Any]:
        """
        Get statistics about AI analyses

        Returns:
            Dictionary with analysis statistics
        """
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*) as total_analyses,
                SUM(CASE WHEN analysis_mode = 'local-claude-code' THEN 1 ELSE 0 END) as local_count,
                SUM(CASE WHEN analysis_mode = 'anthropic-api' THEN 1 ELSE 0 END) as api_count,
                SUM(cost) as total_cost
            FROM ai_analyses
        """)

        row = cursor.fetchone()
        if row:
            stats = dict(row)
            stats['savings'] = stats['local_count'] * 0.024 if stats['local_count'] else 0
            return stats

        return {
            'total_analyses': 0,
            'local_count': 0,
            'api_count': 0,
            'total_cost': 0,
            'savings': 0
        }

    def get_affected_platforms(
        self,
        test_name: str,
        version: str,
        days: int = 7
    ) -> List[str]:
        """
        Get all platforms where a test has failed

        Args:
            test_name: Test name
            version: OpenShift version
            days: How many days back to look

        Returns:
            List of platform names
        """
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT DISTINCT platform
            FROM test_results
            WHERE test_name = ?
            AND version = ?
            AND status = 'failed'
            AND timestamp >= datetime('now', ? || ' days')
            ORDER BY platform
        """, (test_name, version, f'-{days}'))

        return [row[0] for row in cursor.fetchall()]

    def get_enriched_test_results(
        self,
        days: int = 30,
        operator: Optional[str] = None,
        version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get test results joined with job_runs enriched metadata."""
        cursor = self.conn.cursor()

        query = """
            SELECT
                tr.test_name,
                tr.test_description,
                tr.operator,
                tr.status AS result,
                tr.polarion_id,
                tr.error_message,
                tr.duration_seconds AS test_duration,
                tr.manual_classification,
                tr.jira_issue_key,
                jr.job_name AS periodic_job,
                jr.timestamp AS run_date,
                jr.duration_seconds AS job_duration,
                jr.version,
                jr.platform,
                jr.ocp_version,
                jr.csv_version,
                jr.fbc_image,
                jr.step_name,
                jr.job_url,
                jr.build_id
            FROM test_results tr
            JOIN job_runs jr ON tr.job_name = jr.job_name AND tr.build_id = jr.build_id
            WHERE tr.timestamp >= datetime('now', ? || ' days')
            AND tr.status != 'skipped'
            AND COALESCE(tr.job_type, 'periodic') = 'periodic'
        """
        params: list = [f'-{days}']

        if operator:
            query += " AND tr.operator = ?"
            params.append(operator)
        if version:
            query += " AND tr.version = ?"
            params.append(version)

        query += " ORDER BY tr.operator, jr.timestamp DESC, tr.test_name"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_presubmit_test_results(
        self,
        days: int = 30,
        operator: Optional[str] = None,
        version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get presubmit test results joined with job_runs metadata."""
        cursor = self.conn.cursor()

        query = """
            SELECT
                tr.test_name,
                tr.test_description,
                tr.operator,
                tr.status AS result,
                tr.polarion_id,
                tr.error_message,
                tr.duration_seconds AS test_duration,
                tr.pr_number,
                jr.job_name,
                jr.timestamp AS run_date,
                jr.duration_seconds AS job_duration,
                jr.version,
                jr.platform,
                jr.ocp_version,
                jr.csv_version,
                jr.fbc_image,
                jr.step_name,
                jr.job_url,
                jr.build_id,
                jr.pr_number AS jr_pr_number,
                jr.pr_author,
                jr.pr_repo,
                jr.gcs_prefix
            FROM test_results tr
            JOIN job_runs jr ON tr.job_name = jr.job_name AND tr.build_id = jr.build_id
            WHERE tr.timestamp >= datetime('now', ? || ' days')
            AND tr.status != 'skipped'
            AND COALESCE(tr.job_type, 'periodic') = 'presubmit'
        """
        params: list = [f'-{days}']

        if operator:
            query += " AND tr.operator = ?"
            params.append(operator)
        if version:
            query += " AND tr.version = ?"
            params.append(version)

        query += " ORDER BY jr.timestamp DESC, tr.operator, tr.test_name"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_presubmit_job_runs(
        self,
        days: int = 30,
        operator: Optional[str] = None,
        version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get presubmit job run history."""
        cursor = self.conn.cursor()

        query = """
            SELECT
                jr.job_name,
                jr.build_id,
                jr.status,
                jr.timestamp AS run_date,
                jr.duration_seconds,
                jr.version,
                jr.platform,
                jr.total_tests,
                jr.passed_tests,
                jr.failed_tests,
                jr.skipped_tests,
                jr.pass_rate,
                jr.job_url,
                jr.ocp_version,
                jr.csv_version,
                jr.fbc_image,
                jr.step_name,
                jr.pr_number,
                jr.pr_author,
                jr.pr_repo,
                jr.gcs_prefix
            FROM job_runs jr
            WHERE jr.timestamp >= datetime('now', ? || ' days')
            AND COALESCE(jr.job_type, 'periodic') = 'presubmit'
        """
        params: list = [f'-{days}']

        if version:
            query += " AND jr.version = ?"
            params.append(version)

        if operator:
            query += """ AND EXISTS (
                SELECT 1 FROM test_results tr
                WHERE tr.job_name = jr.job_name AND tr.build_id = jr.build_id
                AND tr.operator = ?
            )"""
            params.append(operator)

        query += " ORDER BY jr.timestamp DESC"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_operator_stats(
        self,
        days: int = 30,
        version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get per-operator pass/fail counts for charts."""
        cursor = self.conn.cursor()

        query = """
            SELECT
                COALESCE(operator, 'Unknown') AS operator,
                COUNT(*) AS total_tests,
                SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                ROUND(CAST(SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) * 100, 1) AS pass_rate
            FROM test_results
            WHERE timestamp >= datetime('now', ? || ' days')
            AND status != 'skipped'
            AND COALESCE(job_type, 'periodic') = 'periodic'
        """
        params: list = [f'-{days}']

        if version:
            query += " AND version = ?"
            params.append(version)

        query += " GROUP BY operator ORDER BY operator"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_job_run_history(
        self,
        days: int = 30,
        operator: Optional[str] = None,
        version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get job run history with enriched metadata."""
        cursor = self.conn.cursor()

        query = """
            SELECT
                jr.job_name,
                jr.build_id,
                jr.status,
                jr.timestamp AS run_date,
                jr.duration_seconds,
                jr.version,
                jr.platform,
                jr.total_tests,
                jr.passed_tests,
                jr.failed_tests,
                jr.skipped_tests,
                jr.pass_rate,
                jr.job_url,
                jr.ocp_version,
                jr.csv_version,
                jr.fbc_image,
                jr.step_name,
                jr.failure_reason,
                jr.failed_step,
                jr.failure_category
            FROM job_runs jr
            WHERE jr.timestamp >= datetime('now', ? || ' days')
            AND COALESCE(jr.job_type, 'periodic') = 'periodic'
        """
        params: list = [f'-{days}']

        if version:
            query += " AND jr.version = ?"
            params.append(version)

        if operator:
            query += """ AND EXISTS (
                SELECT 1 FROM test_results tr
                WHERE tr.job_name = jr.job_name AND tr.build_id = jr.build_id
                AND tr.operator = ?
            )"""
            params.append(operator)

        query += " ORDER BY jr.timestamp DESC"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        """Close database connection"""
        self.conn.close()
