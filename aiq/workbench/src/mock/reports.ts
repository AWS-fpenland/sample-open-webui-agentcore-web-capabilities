// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Mock report bodies (markdown, upstream AI-Q shape: H1 title, numbered H2 sections, [n] markers, trailing Sources list).

export const EU_AI_ACT_A = `# EU AI Act obligations for university research groups

## Summary

The EU AI Act (Regulation (EU) 2024/1689) exempts AI systems developed and put into service for the sole purpose of scientific research and development from most obligations, but the exemption ends the moment a system is placed on the market or used in real-world conditions [1][4]. University groups that run pilots with students, patients or public-sector partners are therefore frequently inside scope, and several education use cases are listed as high-risk in Annex III [2].

## 1. Scope and the research exemption

Article 2(6) excludes systems "specifically developed and put into service for the sole purpose of scientific research and development" [1]. Recital 25 clarifies that this covers product-oriented research only until the system is tested in real-world conditions, and that research must still respect Union ethical and professional standards [1][4]. Open-source components released without commercial intent enjoy a separate, narrower carve-out that does not apply to high-risk systems [4].

## 2. High-risk classification in education

Annex III lists AI systems intended to determine access or admission to educational institutions, to evaluate learning outcomes, to assess the appropriate level of education, and to monitor prohibited behaviour during tests [2]. Providers of such systems must implement a risk-management system, data-governance measures, technical documentation, logging, human oversight and a conformity assessment before deployment [2][9]. Deployers — including universities using a third-party system — carry their own duties: human oversight, input-data relevance, monitoring and informing affected students [9].

## 3. Timeline

Prohibited-practice rules applied from 2 February 2025; obligations for general-purpose AI models from 2 August 2025; most high-risk obligations apply from 2 August 2026, with Annex I product-embedded systems following on 2 August 2027 [3][5].

![Figure 1 — Obligation timeline by risk class](artifact://art_9c0d1e2f3a4b5c6d7e8f901a2b3c4d5e)

## 4. What research offices should do now

Map every AI system in use or under development to a risk class, record whether the research exemption still applies, and appoint an owner for documentation [7]. LERU's position paper recommends a lightweight internal register so that the transition from "research" to "deployment" is a recorded decision rather than an accident [7].

## Sources

[1] https://eur-lex.europa.eu/eli/reg/2024/1689/oj — Regulation (EU) 2024/1689, Article 2(6)
[2] https://eur-lex.europa.eu/eli/reg/2024/1689/oj#anx_III — Annex III
[3] https://artificialintelligenceact.eu/implementation-timeline/ — Implementation timeline
[4] https://artificialintelligenceact.eu/recital/25/ — Recital 25
[5] https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai — Regulatory framework for AI
[7] https://www.leru.org/publications — LERU position paper on the AI Act
[9] https://artificialintelligenceact.eu/article/26/ — Article 26, obligations of deployers
`;

export const EU_AI_ACT_B = `# EU AI Act obligations for university research groups

## Summary

Research groups are exempt from most of the Act only while a system is used for the sole purpose of scientific research; real-world testing, pilots with students and any placing on the market end the exemption [1][4]. Where the group also trains or releases general-purpose models, Chapter V obligations apply regardless of the research exemption [12].

## 1. Scope, the research exemption and the timeline

Article 2(6) carves research and development out of the Regulation, but Recital 25 limits the carve-out to activity before real-world use [1][4]. Prohibited-practice rules applied from 2 February 2025; GPAI model obligations from 2 August 2025; high-risk obligations under Annex III apply from 2 August 2026, and Annex I product-embedded systems from 2 August 2027 [3][5].

## 2. High-risk classification in education

Annex III lists admission, assessment, level-placement and exam-proctoring systems as high-risk [2]. Providers must implement risk management, data governance, technical documentation, logging, human oversight and conformity assessment; deployers must ensure oversight and inform affected persons [2][9].

## 2a. Obligations for GPAI model providers

A university that trains and releases a general-purpose model is a *provider* under Chapter V: technical documentation, a copyright policy and a public training-data summary are required, with lighter duties for open-source releases below the systemic-risk threshold [12]. The Commission's July 2025 guidelines confirm that fine-tuning above a compute threshold makes the downstream party a provider too [12].

## 5. Practical checklist for research offices

1. Register every AI system with its risk class and the date real-world use began [7].
2. Decide, in writing, when a research prototype becomes a deployed system [7].
3. Treat regulatory sandbox participation as a documentation duty, not a waiver: sandbox participation does not waive documentation duties [12].
4. Review procurement contracts so that third-party providers carry Annex III provider duties [9].

## Sources

[1] https://eur-lex.europa.eu/eli/reg/2024/1689/oj — Regulation (EU) 2024/1689, Article 2(6)
[2] https://eur-lex.europa.eu/eli/reg/2024/1689/oj#anx_III — Annex III
[3] https://artificialintelligenceact.eu/implementation-timeline/ — Implementation timeline
[4] https://artificialintelligenceact.eu/recital/25/ — Recital 25
[5] https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai — Regulatory framework for AI
[7] https://www.leru.org/publications — LERU position paper on the AI Act
[9] https://artificialintelligenceact.eu/article/26/ — Article 26, obligations of deployers
[12] https://digital-strategy.ec.europa.eu/en/policies/guidelines-gpai-providers — Commission guidelines on GPAI providers (July 2025)
`;

export const S3_VECTORS = `# S3 Vectors vs OpenSearch Serverless for small RAG workloads

## Summary

For under one million vectors with modest query rates, Amazon S3 Vectors is materially cheaper because it has no idle cost and bills per PUT/query [2]. OpenSearch Serverless wins on hybrid (lexical + vector) search, richer filtering and sub-100 ms latency, at the price of a minimum OCU footprint that dominates small deployments [2][4].

## Cost model

S3 Vectors bills storage per logical GB and requests per operation; a 500K-vector index of 1024-dimension float32 embeddings stores roughly 2 GB and costs a few dollars a month at low QPS [1][2]. OpenSearch Serverless bills OCUs per hour with a minimum footprint for indexing and search; even an idle collection costs on the order of a few hundred dollars a month unless the dev/test minimum is used [2].

## Latency and filtering

S3 Vectors targets sub-second query latency with metadata filters limited to 2 KB of filterable metadata per vector [1]. OpenSearch Serverless offers k-NN with pre- and post-filtering, hybrid BM25 + vector ranking and typical p50 latencies under 100 ms [4].

## Limits

An S3 vector index accepts up to 50 million vectors, with dimensions up to 4096 and filterable metadata capped per vector [1]. OpenSearch Serverless scales horizontally by OCU and has no comparable per-index cap [4].

![Monthly cost by vector count](artifact://art_5e6f708192a3b4c5d6e7f8091a2b3c4d)

## Recommendation

Start on S3 Vectors for archives, notebooks and low-QPS assistants; move to OpenSearch Serverless when hybrid search, tight latency budgets or high concurrency become requirements [2][4].

## Sources

[1] https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html — Limitations and restrictions, Amazon S3 Vectors
[2] https://aws.amazon.com/opensearch-service/pricing/ — Amazon OpenSearch Service pricing
[4] https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html — Vector search collections
`;

export const SOC_LANDSCAPE = `# Agentic SOC market landscape 2026

## Summary

The agentic SOC category consolidated in 2026 around three archetypes: SIEM vendors adding autonomous triage, cloud providers exposing security agents on their own telemetry, and startups selling investigation agents that sit above existing tooling [1][3]. Buyers report the largest measurable gains in tier-1 alert triage, where mean time to acknowledge fell by 40–70% in published case studies [2][5].

## 1. Archetypes

Incumbent SIEM platforms ship agents that summarise, cluster and close alerts inside the console [1]. Cloud providers publish agents with read-only access to their own security services and a remediation lane gated by human approval [3]. Independents differentiate on cross-tool investigation and on writing the case narrative [4].

## 2. Where the value shows up

Published deployments show triage volume reductions of 60–85% and analyst hours redirected to hunting [2][5]. Remediation automation remains gated: most references keep a human approval step for any write action [3][5].

## 3. Procurement questions

Ask for the evaluation set the vendor used, the false-negative rate on that set, and how the agent's reasoning is logged for audit [4][6].

![Vendor archetypes by autonomy level](artifact://art_1a2b3c4d5e6f708192a3b4c5d6e7f809)

## Sources

[1] https://www.gartner.com/en/documents/security-operations-2026 — Security operations market guide
[2] https://aws.amazon.com/blogs/security/ — AWS Security Blog: agentic triage case study
[3] https://docs.aws.amazon.com/security-hub/latest/userguide/what-is-securityhub.html — Security Hub documentation
[4] https://www.sans.org/white-papers/ — SANS survey on AI in the SOC
[5] https://www.crowdstrike.com/resources/ — Vendor case studies
[6] https://csrc.nist.gov/publications — NIST guidance on AI in security operations
`;

export const PRICING_QUICK = `# What changed in Bedrock pricing this month?

Two changes landed in the September Price List files: Nova 2 Lite gained a global cross-Region rate ($0.30 in / $2.50 out per 1M tokens) alongside the in-Region rate [1], and several third-party models moved from Marketplace-only listings to first-party Bedrock rates [2]. Batch and flex tiers were extended to more Nova models [1][3].

## Sources

[1] https://aws.amazon.com/bedrock/pricing/ — Amazon Bedrock pricing
[2] https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html — Supported foundation models
[3] https://aws.amazon.com/about-aws/whats-new/ — What's New with AWS
`;

export const FOLLOWUP_FILTERING = `# S3 Vectors filtering and metadata limits — follow-up

## Summary

Filterable metadata is capped at 2 KB per vector and non-filterable metadata at 40 KB; keys must be declared at index creation [1]. Filters support equality, set membership and numeric ranges, but not full-text predicates [1][2].

## Practical guidance

Put tenant ids, document types and dates into filterable metadata; keep long text in non-filterable metadata and rely on the returned keys for hydration [1]. For hybrid needs, pair S3 Vectors with a lexical index or move to OpenSearch Serverless [2].

## Sources

[1] https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html — Limitations and restrictions, Amazon S3 Vectors
[2] https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html — Metadata filtering
`;

export const NOVA_REGION = `# Is Nova 2 Lite available in eu-central-1?

Nova 2 Lite is offered in eu-central-1 through the EU cross-Region inference profile (eu.amazon.nova-2-lite-v1:0); in-Region invocation without a profile is listed for a smaller set of Regions [1][2].

## Sources

[1] https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html — Model support by AWS Region
[2] https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html — Supported cross-Region inference profiles
`;

export function genericReport(title: string, question: string, n: number): string {
  const secs = ['Context', 'Findings', 'Trade-offs', 'Recommendation'];
  const body = secs
    .map((s, i) => `## ${i + 1}. ${s}\n\n${title} — ${s.toLowerCase()} for the question "${question}". The evidence gathered by the researchers supports this section [${(i % n) + 1}][${((i + 1) % n) + 1}].`)
    .join('\n\n');
  const sources = Array.from({ length: n }, (_, i) => `[${i + 1}] https://docs.aws.amazon.com/topic-${i + 1}/ — Source ${i + 1}`).join('\n');
  return `# ${title}\n\n## Summary\n\nA short synthesis of what was found [1].\n\n${body}\n\n## Sources\n\n${sources}\n`;
}
