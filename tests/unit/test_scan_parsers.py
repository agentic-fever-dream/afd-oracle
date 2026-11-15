"""Scanner output parsers: each scanner's JSON becomes normalised findings; secret values are never kept."""
from afd_oracle_ingest import scan


def test_semgrep():
    f = scan.parse_semgrep({"results": [{"check_id": "python.lang.security.audit.eval-detected", "path": "/src/app/x.py",
                                         "start": {"line": 3}, "end": {"line": 3},
                                         "extra": {"message": "Detected eval", "severity": "WARNING",
                                                   "metadata": {"cwe": ["CWE-95"], "references": ["https://x"]}}}]})
    assert (f[0].path, f[0].line, f[0].severity, f[0].cwe) == ("app/x.py", 3, "medium", "CWE-95")


def test_trivy_vuln_misconfig_secret():
    f = scan.parse_trivy({"Results": [
        {"Target": "requirements.txt", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-2023-1234", "PkgName": "jinja2", "InstalledVersion": "2.10", "FixedVersion": "2.11.3",
             "Severity": "HIGH", "Title": "XSS", "CweIDs": ["CWE-79"], "PrimaryURL": "https://avd"}]},
        {"Target": "Dockerfile", "Misconfigurations": [
            {"AVDID": "AVD-DS-0002", "Title": "Image user should not be root", "Severity": "HIGH", "Status": "FAIL",
             "CauseMetadata": {"StartLine": 1, "EndLine": 1}}]},
        {"Target": "app/settings.py", "Secrets": [
            {"RuleID": "aws-access-key-id", "Category": "AWS", "Severity": "CRITICAL", "Title": "AWS key",
             "StartLine": 4, "EndLine": 4, "Match": "AKIA****************"}]}]})
    assert [(x.rule, x.severity) for x in f] == [("CVE-2023-1234", "high"), ("AVD-DS-0002", "high"),
                                                 ("aws-access-key-id", "critical")]
    assert f[0].package == "jinja2" and f[0].fixed == "2.11.3" and f[0].cve == "CVE-2023-1234"
    assert "AKIA" not in f[2].message and "AKIA" not in f[2].title


def test_gitleaks_never_keeps_the_secret():
    f = scan.parse_gitleaks([{"RuleID": "generic-api-key", "Description": "Generic API Key", "File": "/src/conf.py",
                              "StartLine": 2, "EndLine": 2, "Secret": "s3cr3t-value", "Match": "key = s3cr3t-value"}])
    assert f[0].path == "conf.py" and "s3cr3t" not in (f[0].title + f[0].message)


def test_bandit_checkov_hadolint_osv_snyk():
    b = scan.parse_bandit({"results": [{"filename": "/src/a.py", "line_number": 9, "line_range": [9, 10],
                                        "issue_severity": "HIGH", "issue_confidence": "HIGH", "issue_text": "subprocess shell",
                                        "test_id": "B602", "test_name": "subprocess_popen_with_shell_equals_true",
                                        "issue_cwe": {"id": 78}}]})
    assert (b[0].severity, b[0].cwe, b[0].end_line) == ("high", "CWE-78", 10)
    c = scan.parse_checkov([{"check_type": "terraform", "results": {"failed_checks": [
        {"check_id": "CKV_AWS_20", "check_name": "S3 public read", "file_path": "/main.tf", "repo_file_path": "/main.tf",
         "file_line_range": [1, 8], "severity": None, "resource": "aws_s3_bucket.b"}]}}])
    assert (c[0].rule, c[0].path, c[0].line, c[0].severity) == ("CKV_AWS_20", "main.tf", 1, "medium")
    h = scan.parse_hadolint([{"code": "DL3007", "file": "/src/Dockerfile", "level": "warning", "line": 1,
                              "message": "Using latest"}])
    assert (h[0].path, h[0].severity) == ("Dockerfile", "medium")
    o = scan.parse_osv({"results": [{"source": {"path": "/src/requirements.txt"}, "packages": [
        {"package": {"name": "werkzeug", "version": "0.15", "ecosystem": "PyPI"},
         "vulnerabilities": [{"id": "GHSA-xxxx", "aliases": ["CVE-2023-25577"], "summary": "DoS"}],
         "groups": [{"ids": ["GHSA-xxxx"], "max_severity": "7.5"}]}]}]})
    assert (o[0].severity, o[0].cve, o[0].package) == ("high", "CVE-2023-25577", "werkzeug")
    s = scan.parse_snyk({"displayTargetFile": "package.json", "vulnerabilities": [
        {"id": "SNYK-JS-AXIOS-1", "title": "SSRF", "severity": "medium", "packageName": "axios", "version": "0.21.0",
         "fixedIn": ["0.21.1"], "identifiers": {"CVE": ["CVE-2020-28168"], "CWE": ["CWE-918"]}}]})
    assert (s[0].fixed, s[0].cve) == ("0.21.1", "CVE-2020-28168")


def test_ids_are_stable_and_distinct():
    a = scan.Finding("bandit", "B602", "high", "x", "a.py", 9)
    assert a.id == scan.Finding("bandit", "B602", "high", "changed title", "a.py", 9).id
    assert a.id != scan.Finding("bandit", "B602", "high", "x", "a.py", 10).id
