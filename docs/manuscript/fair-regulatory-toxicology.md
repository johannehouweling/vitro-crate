## Regulatory Toxicology

Regulatory toxicology has increasingly adopted FAIR principles to improve the transparency,
consistency and reuse of data submitted for chemical safety assessment. The framing,
however, differs from that used for research data. A research dataset is FAIR when a
competent peer can find it, obtain it and build on it; a regulatory dataset must in addition
support an independent, auditable re-derivation of a decision with legal consequences,
frequently in a jurisdiction other than the one that generated it. Unlike research datasets,
regulatory submissions therefore require highly structured metadata, standardised reporting
formats and traceable provenance — study identity, test facility, GLP status, protocol
deviations, and the versions of guideline and reporting standard applied — to enable
independent evaluation and regulatory decision-making (Briggs et al., 2021). Two further
asymmetries follow. Accessibility is conditional rather than open, since much of the
underlying evidence is claimed as confidential business information and is disclosed to
defined actors under defined conditions. And reusability is inseparable from documented
reliability and relevance: a submission that is well-structured but whose test system,
exposure design and acceptance criteria cannot be reconstructed is unusable for its purpose,
however it scores against generic FAIR metrics.

This structure exists because regulatory toxicology is governed by binding international
agreement. Under the OECD Decision on the Mutual Acceptance of Data (MAD), adopted in 1981,
data generated in one adhering country in accordance with OECD Test Guidelines and the
Principles of Good Laboratory Practice must be accepted by all others for health and
environmental assessment; MAD covers the 38 OECD members and seven non-member adherents, and
has been open to non-members since 1997 (OECD, 1981). Interoperability of regulatory data is
thus not an efficiency but a condition of an international agreement, which is what
distinguishes this subdomain from those described above.

The Organisation for Economic Co-operation and Development (OECD) has played a central role
through the development of the OECD Harmonised Templates (OHTs), which provide structured
machine-readable templates for reporting physicochemical properties, toxicity studies,
environmental fate and ecotoxicity data. These templates underpin regulatory submissions
through platforms such as IUCLID — the submission format for REACH and mandatory for
Australia's AICIS — and facilitate harmonised reporting across international regulatory
authorities, with records surfaced globally through eChemPortal. Their extension to NAMs is
recent and deliberate: OHT 201 ("Intermediate effects") admits non-apical observations at
molecular, subcellular, cellular, tissue and organ level from *in vitro*, *ex vivo* and
*in silico* methods into the same dossier structure as guideline study summaries
(Carnesecchi et al., 2023). EFSA's migration of OpenFoodTox into IUCLID 6 and the OHTs
illustrates convergence on this carrier rather than proliferation away from it (Benfenati et
al., 2026). The Standard for Exchange of Nonclinical Data (SEND), developed by the Clinical
Data Interchange Standards Consortium (CDISC), further standardises the organisation and
exchange of non-clinical toxicology studies submitted to regulatory agencies. SEND defines
common terminology, controlled vocabularies and standardised data structures, enabling
automated validation, data integration and cross-study comparisons while supporting
long-term reuse of regulatory datasets. Uniquely among the resources described in this
paper, it is mandatory: the US FDA has required SEND for nonclinical studies since 2016–2017
depending on submission type, and screens submissions against technical rejection criteria
(FDA, 2024). Both instruments also mark the limits of retrospective standardisation. OHTs
carry robust study summaries rather than raw or processed data, so traceability stops at the
reported result, and SEND models the conventional *in vivo* study and does not accommodate
plate-based designs, high-dimensional omics or computational predictions.

### FAIR data as a precondition for NAM uptake

The increasing adoption of new approach methodologies (NAMs) for regulatory decision-making
(Schmeisser et al., 2023) has highlighted the importance of FAIR data to ensure that diverse
evidence streams can be interpreted, integrated and reused throughout chemical safety
assessment (Watford et al., 2019). Unlike traditional animal studies, NAMs generate
heterogeneous datasets spanning high-content imaging, transcriptomics, metabolomics,
*in vitro* assays, organ-on-chip models and computational predictions (Colbourne et al.,
2025), platforms whose disparate formats, metadata structures and normalisation approaches
directly complicate integration (Sheng et al., 2025). Their regulatory acceptance therefore
depends not only on scientific validity but also on standardised metadata, transparent
provenance, interoperable data formats and reproducible analytical workflows: the
scientific-confidence framework for NAMs rests on fitness for purpose, human biological
relevance, technical characterisation, data integrity and transparency, and independent
review (van der Zalm et al., 2022), and inter-laboratory reproducibility is treated as a
component of confidence distinct from validity (Jacobs et al., 2024). Notably, much of this
regulatory literature imposes requirements that are functionally equivalent to FAIR without
invoking the term, framing them instead as documentation, transparency or reporting
completeness — a divergence in vocabulary that obscures how far the two agendas already
coincide.

Projects such as eTRANSAFE have extended FAIR implementation beyond regulatory reporting by
developing an interoperable knowledge infrastructure for translational drug safety
assessment. The project established a federated Knowledge Hub integrating public and
proprietary preclinical and clinical safety data using ontology services, identifier
management, semantic integration and computational workflows (Lauer et al., 2022).
Federation is the substantive design choice: identifiers and semantics are harmonised so
that analyses execute across institutional boundaries while competitively sensitive data
remain under their owners' control, which is a working answer to the conditional
accessibility described above and transferable to NAM data generated in industry–academia
consortia. In addition to the infrastructure, eTRANSAFE produced FAIR data-sharing
guidelines, research reproducibility guidelines and model verification guidelines to support
consistent stewardship of regulatory toxicology data (Briggs et al., 2021), while also
converting legacy toxicology studies into CDISC SEND to facilitate harmonised data exchange
across pharmaceutical partners and regulatory stakeholders.

### Academic and non-standard data as regulatory evidence

Besides formal submissions and NAM databases, a large body of academic data exists. Such
data are typically not generated under Good Laboratory Practice or to a specific OECD Test
Guideline — and therefore fall outside MAD — but may carry mechanistic, hazard or exposure
information valuable for regulatory decision-making, and for endpoints poorly served by
guideline studies may be the only evidence available. Because public funding policies now
generally adhere to the FAIR principles, such academic and other non-standard data are
expected to be increasingly accessible and reusable. Accessibility, however, is not the
binding constraint; reporting completeness is. This is addressed by the OECD *Guidance
Document on the Generation, Reporting and Use of Research Data for Regulatory Assessments*
(OECD, 2025), which is structured around the research-data lifecycle and assigns
recommendations to distinct actors — funders, researchers, publishers, repository managers,
assessors and risk managers — thereby placing obligations at the point of generation rather
than asking assessors to repair under-documented studies downstream. For data already in
hand, structured appraisal instruments determine how they can be assessed for relevance and
reliability: the Klimisch categories (Klimisch et al., 1997), operationalised by ToxRTool
(Schneider et al., 2009), the CRED criteria (Moermond et al., 2016) and the SciRAP platform
(Beronius et al., 2018). These are complementary to FAIR rather than substitutes for it, and
illustrate the general point developed later in this paper: FAIRness determines whether a
study can be located and interrogated at acceptable cost, these instruments whether it can
be relied upon.

### FAIR NAMs and FAIR NAM-derived data

A distinction frequently collapsed, but consequential for infrastructure design, is that
between making a NAM FAIR and making NAM-derived data FAIR. Making the NAM itself — the
assay, model or protocol — FAIR means describing the method such that it can be assessed
independently of any dataset it produced. Structured templates such as ToxTemp were
developed to satisfy OECD Guidance Document 211 on describing non-guideline *in vitro* test
methods, and define the test-system characterisation, procedural detail and explicit
acceptance criteria required (Krebs et al., 2019); GIVIMP supplies the quality-practice
counterpart, and DB-ALM and TSAR provide method-level findability. The unit of description
is the method, and its persistent identity is what allows independently generated datasets
to be recognised as products of the same procedure. Making NAM-derived data FAIR concerns
the individual experiment instead — which substance, biological model, exposure design,
endpoint, processing pipeline and regulatory question — and is served by the ontologies,
identifiers and packaging conventions described elsewhere in this section. The two are
mutually dependent: a well-described method with no linked data cannot be evaluated on
evidence, and well-packaged data pointing at an under-described method cannot be evaluated
at all. Regulatory infrastructures currently cover these unevenly — IUCLID and the OHTs
standardise the reported result, SEND the study tabulation, GD 211 and ToxTemp the method
description — and none binds method description, data, analysis code and regulatory context
into a single traceable unit.

---

### References for this section

- Benfenati et al. (2026), *EFSA Supporting Publications* EN-10099 — https://doi.org/10.2903/sp.efsa.2026.EN-10099
- Beronius et al. (2018), *J Appl Toxicol* 38(12):1460–1470
- Briggs et al. (2021), *ALTEX* 38(2):187–197 — https://doi.org/10.14573/altex.2011181
- Carnesecchi et al. (2023), *Regul Toxicol Pharmacol* 142:105426
- CDISC, SEND — https://www.cdisc.org/standards/foundational/send
- Colbourne et al. (2025), *Environ Toxicol Chem* 44(9):2395–2397 — https://doi.org/10.1093/etojnl/vgae093
- FDA, *Study Data Technical Conformance Guide*
- Jacobs et al. (2024), *Arch Toxicol* 98(7):2047–2063 — https://doi.org/10.1007/s00204-024-03736-z
- Klimisch et al. (1997), *Regul Toxicol Pharmacol* 25(1):1–5
- Krebs et al. (2019), *ALTEX* 36(4):682–699 — https://doi.org/10.14573/altex.1909271 (erratum *ALTEX* 37(1):164)
- Lauer et al. (2022), *F1000Research* 11:287 — https://pmc.ncbi.nlm.nih.gov/articles/PMC9096149/
- Moermond et al. (2016), *Environ Toxicol Chem* 35(5):1297–1309
- OECD (1981), Decision C(81)30/FINAL, Mutual Acceptance of Data (OECD/LEGAL/0194)
- OECD (2025), *Guidance Document on the Generation, Reporting and Use of Research Data for Regulatory Assessments*, No. 417 — https://doi.org/10.1787/8d49ec1d-en
- OECD, IUCLID — https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/international-uniform-chemical-information-database.html
- OECD, Harmonised Templates — https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/harmonised-templates.html
- Schmeisser et al. (2023), *Environment International* 178:108082 — https://doi.org/10.1016/j.envint.2023.108082
- Schneider et al. (2009), *Toxicol Lett* 189(2):138–144
- Sheng et al. (2025), *Arch Toxicol* 99:4697–4707 — https://doi.org/10.1007/s00204-025-04169-y
- van der Zalm et al. (2022), *Arch Toxicol* 96(11):2865–2879 — https://doi.org/10.1007/s00204-022-03365-4
- Watford et al. (2019), *Toxicol Appl Pharmacol* 380:114707 — https://doi.org/10.1016/j.taap.2019.114707

Still unplaced: https://doi.org/10.1016/j.nsa.2026.106998 — title and authors needed.
The EFSA FAIR statement (https://doi.org/10.2903/j.efsa.2025.9741) is about *models* rather
than regulatory data submission; see the note in the offcuts file on where it fits better.
