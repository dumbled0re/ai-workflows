"""Shared infrastructure used by every per-site adapter.

Modules here are deliberately site-agnostic: they handle HTTP sessions,
cookie persistence with rotation, balance scraping with configurable
patterns, Gmail IMAP fetching, click-result tracking, Slack notification,
URL/log redaction, and the recon crawler. Adapters under
``point_sites.adapters.<site>`` import from here and inject their
site-specific URLs / regexes / queries.
"""
