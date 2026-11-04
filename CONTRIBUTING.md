# Contributing to restart-count-exporter

Bug reports, ideas and pull requests are welcome.

## Getting started

- Open an issue for bugs, ideas or questions.
- Fork, branch, and submit a pull request.
- The whole project is `exporter.py` and `test_exporter.py`. The tests stub the Docker API in-process and spin up a fake one on a loopback port and a unix socket, so `python -m unittest discover -p 'test_*.py'` needs no Docker daemon.

## Guidelines

- Keep it small. The exporter is standard library only and renders the exposition format by hand; a new label is a pair in `sample_line`, not a new library.
- A scrape either succeeds whole or fails whole: `collector_up` is the one gauge that is always present, and a failure must never leave stale samples in the body.
- Run the tests before submitting a PR; CI runs them on every pull request, builds the image and scrapes the runner's own Docker daemon through it.
- **We use [release-please](https://github.com/googleapis/release-please).** Write [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, …) so versions and the changelog derive from the history.

## AI policy

AI coding tools are used in this project, steered by the maintainer, and nothing is merged without a human having reviewed it. Contributions written with such tools are welcome under the same rule: you are responsible for what you submit, so understand it and make sure it works.
