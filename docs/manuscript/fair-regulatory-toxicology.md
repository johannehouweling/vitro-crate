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
independent evaluation and regulatory decision-making (Briggs et al., 2021;
doi:10.14573/altex.2011181). Two further asymmetries follow. Accessibility is conditional
rather than open, since much of the underlying evidence is claimed as confidential business
information and is disclosed to defined actors under defined conditions. And reusability is
inseparable from documented reliability and relevance: a submission that is well-structured
but whose test system, exposure design and acceptance criteria cannot be reconstructed is
unusable for its purpose, however it scores against generic FAIR metrics.

This structure exists because regulatory toxicology is governed by binding international
agreement. Under the OECD Decision on the Mutual Acceptance of Data (MAD), adopted in 1981,
data generated in one adhering country in accordance with OECD Test Guidelines and the
Principles of Good Laboratory Practice must be accepted by all others for health and
environmental assessment; MAD covers the 38 OECD members and seven non-member adherents, and
has been open to non-members since 1997 (OECD Council Decision C(81)30/FINAL,
OECD/LEGAL/0194). Interoperability of regulatory data is thus not an efficiency but a
condition of an international agreement, which is what distinguishes this subdomain from
those described above.

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
(Carnesecchi et al., 2023; *Regul Toxicol Pharmacol* 142:105426 —
https://www.sciencedirect.com/science/article/abs/pii/S0273230023000946). EFSA's migration of
OpenFoodTox into IUCLID 6 and the OHTs illustrates convergence on this carrier rather than
proliferation away from it (Benfenati et al., 2026;
doi:10.2903/sp.efsa.2026.EN-10099). The Standard for Exchange of Nonclinical Data (SEND),
developed by the Clinical Data Interchange Standards Consortium (CDISC), further standardises
the organisation and exchange of non-clinical toxicology studies submitted to regulatory
agencies. SEND defines common terminology, controlled vocabularies and standardised data
structures, enabling automated validation, data integration and cross-study comparisons while
supporting long-term reuse of regulatory datasets. Uniquely among the resources described in
this paper, it is mandatory: the US FDA has required SEND for nonclinical studies since
2016–2017 depending on submission type, and screens submissions against technical rejection
criteria (FDA, *Study Data Technical Conformance Guide*). Both instruments also mark the
limits of retrospective standardisation. OHTs carry robust study summaries rather than raw or
processed data, so traceability stops at the reported result, and SEND models the conventional
*in vivo* study and does not accommodate plate-based designs, high-dimensional omics or
computational predictions.

### FAIR data as a precondition for NAM uptake

The increasing adoption of new approach methodologies (NAMs) for regulatory decision-making
(Schmeisser et al., 2023; doi:10.1016/j.envint.2023.108082) has highlighted the importance of
FAIR data to ensure that diverse evidence streams can be interpreted, integrated and reused
throughout chemical safety assessment (Watford et al., 2019;
doi:10.1016/j.taap.2019.114707). Unlike traditional animal studies, NAMs generate
heterogeneous datasets spanning high-content imaging, transcriptomics, metabolomics,
*in vitro* assays, organ-on-chip models and computational predictions (Colbourne et al.,
2025; doi:10.1093/etojnl/vgae093; Sheng et al., 2025;
doi:10.1007/s00204-025-04169-y). Their regulatory acceptance therefore depends not only on
scientific validity but also on standardised metadata, transparent provenance, interoperable
data formats and reproducible analytical workflows: the scientific-confidence framework for
NAMs rests on fitness for purpose, human biological relevance, technical characterisation,
data integrity and transparency, and independent review (van der Zalm et al., 2022;
doi:10.1007/s00204-022-03365-4), and inter-laboratory reproducibility is treated as a
component of confidence distinct from validity (Jacobs et al., 2024;
doi:10.1007/s00204-024-03736-z). Notably, much of this regulatory literature imposes
requirements that are functionally equivalent to FAIR without invoking the term, framing them
instead as documentation, transparency or reporting completeness — a divergence in vocabulary
that obscures how far the two agendas already coincide.

Deepika et al. present FAIRification and harmonisation as enablers of NAM data usability, and
thus of NAM applicability. NAM data are now generated in rapidly growing volumes, but
integrating and using them remains difficult. The authors attribute this to two problems: the
data are heterogeneous in format, structure, and terminology across structured,
semi-structured, and unstructured sources; and their generation and reporting lack
standardisation. This contrasts with animal studies, whose established guidelines support
their use in risk-assessment. Ontology-based approaches are argued as a route to
machine-readable data and models, and thereby to the more reproducible and robust predictive
models that NAMs need to support IATA (Deepika et al., 2025;
doi:10.3389/ftox.2025.1632941).

Projects such as eTRANSAFE have extended FAIR implementation beyond regulatory reporting by
developing an interoperable knowledge infrastructure for translational drug safety
assessment. The project established a federated Knowledge Hub integrating public and
proprietary preclinical and clinical safety data using ontology services, identifier
management, semantic integration and computational workflows (Lauer et al., 2022;
*F1000Research* 11:287 — https://pmc.ncbi.nlm.nih.gov/articles/PMC9096149/). Federation is
the substantive design choice: identifiers and semantics are harmonised so that analyses
execute across institutional boundaries while competitively sensitive data remain under their
owners' control, which is a working answer to the conditional accessibility described above
and transferable to NAM data generated in industry–academia consortia. In addition to the
infrastructure, eTRANSAFE produced FAIR data-sharing guidelines, research reproducibility
guidelines and model verification guidelines to support consistent stewardship of regulatory
toxicology data (Briggs et al., 2021; doi:10.14573/altex.2011181), while also converting
legacy toxicology studies into CDISC SEND to facilitate harmonised data exchange across
pharmaceutical partners and regulatory stakeholders.

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
(OECD, 2025, Series on Testing and Assessment No. 417; doi:10.1787/8d49ec1d-en), which is
structured around the research-data lifecycle and assigns recommendations to distinct actors
— funders, researchers, publishers, repository managers, assessors and risk managers —
thereby placing obligations at the point of generation rather than asking assessors to repair
under-documented studies downstream. For data already in hand, structured appraisal
instruments determine how they can be assessed for relevance and reliability: the Klimisch
categories (Klimisch et al., 1997; doi:10.1006/rtph.1996.1076), operationalised by ToxRTool
(Schneider et al., 2009; doi:10.1016/j.toxlet.2009.05.013), the CRED criteria for ecotoxicity
data (Moermond et al., 2016; doi:10.1002/etc.3259) and the SciRAP platform, whose *in vitro*
tools extend an approach first developed for *in vivo* studies (Roth et al., 2021;
doi:10.3389/ftox.2021.746430; Beronius et al., 2018; doi:10.1002/jat.3648). These are
complementary to FAIR rather than substitutes for
it, and illustrate the general point developed later in this paper: FAIRness determines
whether a study can be located and interrogated at acceptable cost, these instruments whether
it can be relied upon.

### FAIR NAMs and FAIR NAM-derived data

A distinction frequently collapsed, but consequential for infrastructure design, is that
between making a NAM FAIR and making NAM-derived data FAIR. Making the NAM itself — the
assay, model or protocol — FAIR means describing the method such that it can be assessed
independently of any dataset it produced. Structured templates such as ToxTemp were
developed to satisfy OECD Guidance Document 211 on describing non-guideline *in vitro* test
methods (OECD, 2017, Series on Testing and Assessment No. 211;
doi:10.1787/9789264274730-en; issued as ENV/JM/MONO(2014)35), and define the test-system
characterisation, procedural detail and explicit
acceptance criteria required (Krebs et al., 2019; doi:10.14573/altex.1909271, erratum
doi:10.14573/altex.1909271e); GIVIMP supplies the quality-practice counterpart (OECD Series
on Testing and Assessment No. 286; doi:10.1787/9789264304796-en; second edition 2025, No.
421, doi:10.1787/5ba6777b-en). Method-level findability is provided by TSAR, the Tracking
System for Alternative methods towards Regulatory acceptance maintained by EURL ECVAM at the
JRC, which follows a method from submission through validation and peer review to regulatory
acceptance; its predecessor protocol database DB-ALM was archived in 2019 and survives only
as a static dataset, itself an illustration of how difficult sustained curation of
method-level resources has proved. The unit
of description is the method, and its persistent identity is what allows independently
generated datasets to be recognised as products of the same procedure. Making NAM-derived
data FAIR concerns the individual experiment instead — which substance, biological model,
exposure design, endpoint, processing pipeline and regulatory question — and is served by the
ontologies, identifiers and packaging conventions described elsewhere in this section. The
two are mutually dependent: a well-described method with no linked data cannot be evaluated
on evidence, and well-packaged data pointing at an under-described method cannot be evaluated
at all. Regulatory infrastructures currently cover these unevenly — IUCLID and the OHTs
standardise the reported result, SEND the study tabulation, GD 211 and ToxTemp the method
description — and none binds method description, data, analysis code and regulatory context
into a single traceable unit.

---

### Web resources cited above (no DOI)

- OECD Harmonised Templates — https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/harmonised-templates.html
- OECD IUCLID — https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/international-uniform-chemical-information-database.html
- OECD Mutual Acceptance of Data — https://www.oecd.org/en/topics/sub-issues/testing-of-chemicals/mutual-acceptance-of-data-system.html
- CDISC SEND — https://www.cdisc.org/standards/foundational/send
- FDA Study Data Technical Conformance Guide — cite the current version

### Verification notes

- TSAR — https://tsar.jrc.ec.europa.eu/ (EURL ECVAM, JRC). DB-ALM archived 2019; static
  dataset at http://data.europa.eu/89h/b7597ada-148d-4560-9079-ab0a5539cad3
- Author lists confirmed: Moermond, Kase, Korkaric & Ågerstrand (four, no *et al.*);
  Schneider, Schwarz, Burkholder, Kopp-Schneider, Edler, Kinsner-Ovaskainen, Hartung &
  Hoffmann (eight); Beronius, Molander, Zilliacus, Rudén & Hanberg (five); Roth, Zilliacus &
  Beronius (three).
- Kase et al. 2016 (*Environ Sci Eur* 28:7, doi:10.1186/s12302-016-0073-x) is a ring test
  comparing CRED with Klimisch — cite only as evidence of uptake, not as the CRED method
  source.

### Still outstanding

- **Carnesecchi et al. 2023** — article number 105426 verified; DOI not seen directly.
  Expected form `10.1016/j.yrtph.2023.105426`, confirm before use.
- **Lauer et al. 2022** — F1000Research DOI not captured; PMC9096149 / PMID 35602243 verified.
- **https://doi.org/10.1016/j.nsa.2026.106998** — still unidentified; title and authors needed
  before it can be placed.
