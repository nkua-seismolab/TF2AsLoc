# TF2AsLoc

[![lint](https://github.com/nkua-seismolab/TF2AsLoc/actions/workflows/lint.yml/badge.svg)](https://github.com/nkua-seismolab/TF2AsLoc/actions/workflows/lint.yml)
[![tests](https://github.com/nkua-seismolab/TF2AsLoc/actions/workflows/tests.yml/badge.svg)](https://github.com/nkua-seismolab/TF2AsLoc/actions/workflows/tests.yml)
[![docs](https://readthedocs.org/projects/tf2asloc/badge/?version=latest)](https://tf2asloc.readthedocs.io/en/latest/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

TF2AsLoc (TRANSFORM2 Associator-Locator) is a near real-time seismic processing service. It accepts phase picks over a REST API, associates them into events with [GaMMA](https://github.com/AI4EPS/GaMMA), computes magnitudes, locates the events with [USGS HypoInverse](https://www.usgs.gov/software/hypoinverse-earthquake-location), and returns the results as QuakeML.

**Full documentation:** https://tf2asloc.readthedocs.io

## Requirements

- Docker and Docker Compose (everything runs in containers; no host Python needed).

## Quick start

Please see the documentation for a quick start guide.

## Demo

A complete example - a picks package (`.json`), a demo `config.yaml`, all necessary input files, and the expected QuakeML output - is archived on [Zenodo](https://doi.org/10.5281/zenodo.23082786).

You will still need to adjust some parameters in the provided demo `config.yaml` for your setup; the full walkthrough is in the [documentation](https://tf2asloc.readthedocs.io/en/latest/demo/).

## License

[GPL-3.0](LICENSE)

## Funding

This work is part of the [TRANSFORM²](https://www.transform2-project.eu/) project which aims to improve physical and digital infrastructure across Near-Fault Observatories (NFOs) in Europe.

TRANSFORM² is funded by the European Union under project number 101188365 within the HORIZON-INFRA-2024-DEV-01-01 call.

<div align="center">
  <img src="https://www.transform2-project.eu/wp-content/uploads/2022/08/Logo_TRANSFORM2-round-logo-100x100-1.png" alt="TRANSFORM² logo">
</div>