# Security Policy

## Supported Versions

If vulnerabilities are detected in dependencies or the app itself,
we will release a fix to patch them and release it as a new version.
For example, if a vulnerability was found in version 3.1.4 we would
fix that and release version 3.1.5. Any version below version 5.0.0
would now be considered deprecated and should not be used. Update
to one of the newer versions through home assistant OS.

All of our releases undergo strict CI and security scanning before
being released. We use tools such as bandit, trivy, sonarqube, and
more!

| Version | Supported          |
| ------- | ------------------ |
| > 6.0.0 | :white_check_mark: |
| 5.0.0   | :white_check_mark: |
| 4.0.0   | :x:                |
| < 3.0.0 | :x:                |

## Reporting a Vulnerability

To report an issue of vulnerability, either create a new issue in the
issues page or contact me via email. My email contact information
can be found in the home assistant app and in the repository.
