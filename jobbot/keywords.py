"""Keyword bank for JD analysis, scoring and the resume truthfulness guard."""
from __future__ import annotations

import re

PRODUCT = [
    "product management", "product roadmap", "roadmap", "user stories", "user story", "prd",
    "requirements gathering", "requirements analysis", "backlog", "agile", "scrum",
    "sprint planning", "stakeholder management", "go-to-market", "kpi", "a/b testing",
    "product analytics", "user research", "product lifecycle", "product strategy",
    "prioritization", "product adoption", "functional documentation", "business analysis",
    "use cases", "workflow analysis", "wireframes", "figma", "uat",
    "user acceptance testing", "sow", "bpmn", "uml",
]
TOOLS = [
    "jira", "confluence", "sql", "pl/sql", "mysql", "postgresql", "excel", "power bi",
    "tableau", "python", "java", "javascript", "api", "rest api", "json", "xml", "postman",
    "soapui", "integration", "etl", "data model", "zapier", "aws", "azure", "gcp", "git",
    "docker", "kubernetes", "ci/cd", "jenkins", "selenium", "cypress",
]
SUPPORT = [
    "customer support", "technical support", "application support", "production support",
    "incident management", "ticketing", "zendesk", "freshdesk", "servicenow", "sla",
    "escalation", "root cause analysis", "rca", "troubleshooting", "log analysis", "grafana",
    "kibana", "splunk", "datadog", "new relic", "linux", "unix", "bash", "shell scripting",
    "monitoring", "on-call", "itil", "knowledge base", "customer success", "onboarding",
    "implementation", "go-live", "data migration", "churn", "renewal", "upsell", "nps",
    "csat", "account management", "client communication", "customer query resolution",
    "issue resolution", "saas", "b2b",
]
DOMAIN = [
    "order management", "oms", "order routing", "fulfillment", "fulfilment",
    "inventory management", "inventory", "available to promise", "supply chain",
    "e-commerce", "ecommerce", "retail", "omnichannel", "shopify", "netsuite", "sap", "erp",
    "wms", "tms", "pim", "mdm", "oltp", "olap",
]
PERF = [
    "jmeter", "loadrunner", "gatling", "performance testing", "load testing",
    "stress testing", "performance engineering", "sanity testing", "regression testing",
    "test automation", "qa",
]
BANK = sorted(set(PRODUCT + TOOLS + SUPPORT + DOMAIN + PERF))

_PATTERNS = {t: re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?:s|es)?(?![a-z0-9])")
             for t in BANK}
_WS = re.compile(r"\s+")

_SPECIAL = {
    "jmeter": "JMeter", "loadrunner": "LoadRunner", "netsuite": "NetSuite",
    "servicenow": "ServiceNow", "postgresql": "PostgreSQL", "mysql": "MySQL",
    "pl/sql": "PL/SQL", "power bi": "Power BI", "new relic": "New Relic",
    "go-to-market": "Go-to-market", "ci/cd": "CI/CD", "a/b testing": "A/B testing",
    "javascript": "JavaScript", "java": "Java", "jira": "Jira", "git": "Git",
    "saas": "SaaS", "b2b": "B2B", "itil": "ITIL", "kpi": "KPI", "zendesk": "Zendesk",
    "freshdesk": "Freshdesk", "unix": "Unix",
}


def terms_in(text: str) -> set[str]:
    """Canonical bank terms that occur in `text` (case-insensitive, plural-tolerant)."""
    t = _WS.sub(" ", (text or "").lower())
    return {term for term, rx in _PATTERNS.items() if rx.search(t)}


def display(term: str) -> str:
    if term in _SPECIAL:
        return _SPECIAL[term]
    if len(term) <= 4 and " " not in term:
        return term.upper()
    return term.capitalize()


def display_terms(terms) -> str:
    return ", ".join(display(t) for t in sorted(terms))
