# FAIR data in regulatory toxicology

> Enriched draft of the "FAIR toxicology data in a regulatory context" section.
> Inline citations are author–year for easy conversion in Zotero; full details in
> the reference list at the end. Items marked ⚠ still need a metadata check
> (see "Verification status").

---

## 1. Why "FAIR" means something different in a regulatory setting

Regulatory toxicology has increasingly adopted the FAIR principles (Wilkinson et al., 2016)
to improve the transparency, consistency and reuse of data submitted for chemical and
pharmaceutical safety assessment. The framing, however, differs from that used for research
data. A research dataset is FAIR when a competent peer can find it, obtain it, combine it
with other data and build on it. A regulatory dataset must additionally support an
*independent, auditable re-evaluation* of a conclusion that carries legal and economic
consequences: the assessor is not reusing the data to generate a new hypothesis, but
re-deriving a decision from it. This shifts the emphasis of each FAIR facet.

- **Findable** in the regulatory sense means indexed against the substance identity,
  endpoint and legal instrument under which the study was submitted, not merely
  deposited with a DOI.
- **Accessible** is conditional rather than open. Much of the underlying evidence is
  claimed as confidential business information, so regulatory infrastructures implement
  a graded, "as open as possible, as closed as necessary" model in which the metadata
  layer is public while the underlying study record is disclosed to defined actors
  under defined conditions.
- **Interoperable** is achieved by construction, through mandated information models and
  controlled terminologies, rather than negotiated after the fact between data producers.
- **Reusable** is inseparable from documented *reliability and relevance*. A regulatory
  dataset that is technically well-structured but whose test system, exposure design and
  acceptance criteria cannot be reconstructed is not reusable for its intended purpose,
  regardless of how well it scores against generic FAIR metrics.

Regulatory submissions therefore require highly structured metadata, standardised
reporting formats and traceable provenance — including study identity, test facility,
GLP status, protocol deviations, and the version of the guideline and of the reporting
standard applied — to enable independent evaluation and regulatory decision-making
(OECD, 2025; Briggs et al., 2021). It is worth noting that this machine-actionability was
achieved through mandates and tooling rather than through advocacy: in both of the major
regulatory ecosystems described below, structured reporting became routine only once a
shared information model, a validation service and a submission pipeline were made a
condition of filing.

## 2. Harmonised reporting under the OECD: OHTs, IUCLID and eChemPortal

The Organisation for Economic Co-operation and Development (OECD) has played a central role
through the development of the **OECD Harmonised Templates (OHTs)**, which provide structured,
machine-readable templates for reporting physicochemical properties, toxicity studies,
environmental fate and ecotoxicity data. Each template fixes the fields, cardinalities and
picklists for a given endpoint, so that a study summary produced in one jurisdiction can be
parsed and evaluated in another without re-keying or re-interpretation.

These templates are implemented in **IUCLID** (International Uniform ChemicaL Information
Database), maintained jointly by the OECD and the European Chemicals Agency (ECHA), which
serves as the submission format for REACH registrations and is also used for biocides,
plant protection products and by regulatory programmes outside the European Union
(OECD, n.d.). The resulting records are surfaced across jurisdictions through
**eChemPortal**, the OECD global portal to information on chemical substances, and — for
REACH — through ECHA's public dissemination platform, which together constitute the
largest openly queryable body of regulatory toxicology data in existence.

Two features make this ecosystem instructive as a FAIR implementation. First,
interoperability is not an aspiration but a precondition of submission: dossiers are
validated against the template specification before they are accepted. Second, the OHT
suite has been deliberately extended to accommodate mechanistic and non-apical evidence.
**OHT 201 ("Intermediate effects")** was introduced specifically to allow reporting of
observations at the molecular, subcellular, cellular, tissue and organ level obtained from
*in vitro*, *ex vivo* and *in silico* methods, so that NAM-derived evidence can enter the
same dossier structure as guideline study summaries rather than remaining as unstructured
supporting information (⚠ OHT 201 paper, 2023).

The limitations are equally instructive. OHTs capture *robust study summaries* rather than
raw or processed data, so the traceability chain typically stops at the level of the
reported result; template conformance guarantees syntactic but not full semantic
interoperability, since many free-text fields remain; and accessibility is bounded by
confidentiality claims. For NAM data in particular, the template's expressive power
constrains what can be said about a test system that the template's authors did not
anticipate.

## 3. Standardised nonclinical data exchange: CDISC SEND

The **Standard for Exchange of Nonclinical Data (SEND)**, developed by the Clinical Data
Interchange Standards Consortium (CDISC), standardises the organisation and exchange of
non-clinical toxicology studies submitted to regulatory agencies (CDISC, n.d.). SEND is an
implementation of the CDISC Study Data Tabulation Model (SDTM) for nonclinical work: it
defines common terminology, controlled vocabularies and standardised tabular data
structures, together with a machine-readable data definition file, enabling automated
validation, data integration and cross-study comparison while supporting long-term reuse
of regulatory datasets.

Unlike most FAIR initiatives in toxicology, SEND is mandatory. The United States Food and
Drug Administration requires SEND datasets for nonclinical studies starting after
17 December 2016 for NDA, BLA and ANDA submissions, and after 17 December 2017 for INDs,
with the applicable version of the SEND Implementation Guide (SENDIG) stepping from
v3.0 to v3.1 for studies beginning after 15 March 2019 and 15 March 2020 respectively
(FDA Study Data Technical Conformance Guide; CDISC, n.d.). Submissions are screened
against technical rejection criteria, so a non-conformant dataset is not merely
discouraged but returned. Domain-specific extensions such as SENDIG-DART for
developmental and reproductive toxicology broaden the coverage beyond general toxicity
study designs.

The practical consequence is that a decade of nonclinical safety data now exists in a
single, queryable structure. This has enabled uses that were previously impractical:
construction of pooled historical control databases, systematic cross-study and
cross-compound comparison, automated reviewer workflows, and retrospective analyses of
the concordance between animal findings and human outcomes. It also delimits the
challenge for NAMs: SEND models the conventional *in vivo* study — subjects, groups,
findings over time — and does not natively accommodate plate-based *in vitro* designs,
high-dimensional omics readouts or computational predictions. The lesson transfers even
though the schema does not.

## 4. NAMs and the FAIR bottleneck

The increasing adoption of new approach methodologies (NAMs) for regulatory decision-making
has highlighted the importance of FAIR data to ensure that diverse evidence streams can be
interpreted, integrated and reused throughout chemical safety assessment. Unlike traditional
animal studies, NAMs generate heterogeneous datasets spanning high-content imaging,
transcriptomics, metabolomics, *in vitro* assays, organ-on-chip models and computational
predictions. Their regulatory acceptance therefore depends not only on scientific validity
but also on standardised metadata, transparent provenance, interoperable data formats and
reproducible analytical workflows. Recent calls for a unified validation and acceptance
framework make the same point from the quality-assurance side: without measurable,
standardised documentation there is no defensible basis on which to judge whether a method
is fit for a given regulatory purpose (Ouedraogo et al., 2025).

Deepika et al. (2025) present FAIRification and harmonisation as enablers of NAM data
usability, and thus of NAM applicability. NAM data are now generated in rapidly growing
volumes, but integrating and using them remains difficult. The authors attribute this to
two problems: the data are heterogeneous in format, structure and terminology across
structured, semi-structured and unstructured sources; and their generation and reporting
lack standardisation. This contrasts with animal studies, whose established guidelines
support their use in risk assessment. Ontology-based approaches are argued as a route to
machine-readable data and models, and thereby to the more reproducible and robust
predictive models that NAMs need in order to support Integrated Approaches to Testing and
Assessment (IATA) — a route that also conditions the credible use of AI and machine
learning on NAM evidence, since such methods inherit whatever inconsistency is present in
their training data.

A complementary framing is that the problem is not one bottleneck but several, distributed
across the *layers* through which NAM evidence passes. Blum et al. (2025) describe the
route from raw instrument output through processed data to interpreted, decision-relevant
information as a sequence of transformations, each of which discards context that a later
assessor may need. In practice, key contextual information — experimental design, exposure
conditions, biological model characterisation, data-processing steps and the regulatory
question being addressed — is captured inconsistently, dispersed across documents, or held
only in non-machine-readable form (Krebs et al., 2020; Blum et al., 2025). The same
argument has been made for the mechanistic scaffolding on which NAM evidence is hung:
adverse outcome pathways themselves must be FAIR if they are to serve as a stable
interoperability layer between assays and regulatory endpoints (Wittwehr et al., 2024).

The regulatory community has responded with reporting frameworks that are, in effect,
domain-specific metadata standards. **GIVIMP** (OECD Guidance Document No. 286; second
edition 2025) sets out good *in vitro* method practices covering test systems, reagents,
SOPs, method performance and record retention. The **OECD Omics Reporting Framework**
(OORF; OECD Series on Testing and Assessment No. 390, 2023) defines the reporting elements
required for the regulatory use of transcriptomics and metabolomics data from
laboratory-based toxicology studies. For computational methods, the (Q)SAR Model Reporting
Format (QMRF) and Prediction Reporting Format (QPRF) play the analogous role. Each of these
specifies *what must be recorded*; none of them specifies a machine-actionable container in
which the record and the data travel together, which is the gap that packaging standards
address.

## 5. Federated infrastructure for proprietary evidence: eTRANSAFE

Projects such as **eTRANSAFE** have extended FAIR implementation beyond regulatory reporting
by developing an interoperable knowledge infrastructure for translational drug safety
assessment. The project established a federated **Knowledge Hub** integrating public and
proprietary preclinical and clinical safety data using ontology services, identifier
management, semantic integration and computational workflows (Lauer et al., 2022). The
federated architecture is the substantive design choice: rather than requiring
pharmaceutical partners to pool competitively sensitive study data in a central repository,
the infrastructure harmonises identifiers and semantics so that analyses can be executed
across institutional boundaries while the data remain under their owners' control. This is
a concrete answer to the accessibility tension described in Section 1, and a
transferable one for NAM data generated under industry–academia consortia.

In addition to the infrastructure, eTRANSAFE produced FAIR data-sharing guidelines,
research reproducibility guidelines and model verification guidelines to support consistent
stewardship of regulatory toxicology data (Briggs et al., 2021; Lauer et al., 2022). These
guidelines were compiled from feedback across regulatory agencies, industry and academic
partners, and were explicitly written as generic, reusable deliverables rather than
project-internal documentation. The project also converted legacy toxicology studies into
CDISC SEND, retrofitting a standard onto historical data in order to facilitate harmonised
exchange across pharmaceutical partners and regulatory stakeholders — a demonstration that
FAIRification is not restricted to prospectively generated data, albeit at a cost that
prospective standardisation avoids.

## 6. Academic and other non-standard data as regulatory evidence

Besides formal submissions and NAM databases, a large body of academic data exists. Such
data are typically not generated under Good Laboratory Practice or to a specific OECD Test
Guideline, but may carry mechanistic, hazard or exposure information valuable for regulatory
decision-making — and in some cases it is the only available evidence for endpoints, such as
developmental neurotoxicity, that guideline studies address poorly or not at all. Because
public funding policies now generally require adherence to the FAIR principles, and because
journals and funders increasingly mandate data availability statements and deposition, such
academic and other non-standard data are expected to become progressively more accessible
and reusable.

Accessibility, however, is not the binding constraint. The obstacle to regulatory use of
academic data has consistently been *reporting completeness*: assessors cannot judge
reliability and relevance when the test system is under-characterised, the purity and
identity of the test substance are unstated, concentrations are nominal and unverified, or
the statistical treatment cannot be reconstructed. This is addressed directly by the OECD
**Guidance Document on the Generation, Reporting and Use of Research Data for Regulatory
Assessments** (OECD Series on Testing and Assessment No. 417, 2025), which is structured
around the lifecycle of research data — from generation and reporting through
identification, evaluation and integration into regulatory assessments — and issues
practical recommendations to distinct actors: funders, researchers, publishers, repository
managers, assessors and risk managers. Its significance is that it places obligations
upstream, on the point of data generation, rather than asking assessors to repair
under-documented studies downstream.

Where such data are already in hand, structured appraisal instruments provide the bridge
between availability and use. The Klimisch categories (Klimisch et al., 1997) remain the
reference point, and have been operationalised and extended by tools including ToxRTool
(⚠ Schneider et al., 2009), the CRED criteria for ecotoxicity data (⚠ Moermond et al., 2016;
Kase et al., 2016) and the SciRAP platform for *in vivo* and *in vitro* toxicity studies
(⚠ Beronius et al., 2018), each of which separates reliability from relevance and makes the
basis of the judgement explicit. The relationship to FAIR is complementary rather than
substitutive: FAIRness determines whether a study can be *located and interrogated* at
acceptable cost, while these instruments determine whether it can be *relied upon*. A
perfectly FAIR dataset can still be regulatorily unusable, and this distinction is worth
preserving in any claim that FAIRification advances regulatory uptake.

## 7. FAIR methods versus FAIR method-derived data

A distinction that is frequently collapsed, but which matters for both infrastructure design
and evaluation, is that between making a **NAM** FAIR and making **NAM-derived data** FAIR.

Making the NAM itself — the assay, model or protocol — FAIR means describing the method in
sufficient detail that it can be assessed, reproduced and transferred independently of any
particular dataset it has produced. Structured templates such as **ToxTemp** (Krebs et al.,
2019) were developed to satisfy **OECD Guidance Document 211** on describing non-guideline
*in vitro* test methods (OECD, 2017), by decomposing its requirements into concrete
questions covering test-system characterisation, procedural detail and explicit acceptance
criteria. GIVIMP (OECD, 2018) supplies the quality-practice counterpart, and method-level
findability is served by registries such as the EURL ECVAM DataBase service on ALternative
Methods (DB-ALM) and the Tracking System for Alternative methods towards Regulatory
acceptance (TSAR). The unit of description here is the *method*, and its persistent identity
— ideally carried by a resolvable identifier — is what allows independently generated
datasets to be recognised as having been produced by the same procedure.

Making NAM-derived data FAIR is a different problem, concerned with the individual
experiment: which substance was tested, on which biological model, under which exposure
design, measuring which endpoint, processed by which pipeline, and answering which
regulatory question. Here the enabling infrastructure is ontological and structural rather
than narrative — persistent identifiers and controlled vocabularies for chemicals, cell
lines, assays and endpoints (for example the BioAssay Ontology and Cellosaurus), minimum
information models for the relevant data type, and a packaging convention that keeps raw
data, processed data, scripts, protocols and metadata together as one machine-actionable
research object rather than as a folder of files (Soiland-Reyes et al., 2022).

The two are mutually dependent, and a third object completes the set. A well-described
method with no linked data cannot be evaluated on evidence; well-packaged data pointing at
an under-described method cannot be evaluated at all; and both remain irreproducible unless
the *analytical workflow* that turned measurements into the reported result is itself
captured and re-executable. Existing regulatory infrastructures address these unevenly:
IUCLID and the OHTs standardise the reported result, SEND standardises the study
tabulation, GD 211 and ToxTemp standardise the method description, and the OORF standardises
omics reporting — but no single instrument currently binds method description, raw and
processed data, analysis code and regulatory context into one traceable, machine-actionable
unit. That gap is the point of departure for the present work.

---

## References

Beronius, A., Molander, L., Zilliacus, J., Rudén, C., & Hanberg, A. (2018). Testing and
refining the Science in Risk Assessment and Policy (SciRAP) web-based platform for
evaluating the reliability and relevance of *in vivo* toxicity studies. *Journal of Applied
Toxicology*, 38(12), 1460–1470. ⚠ *verify volume/pages*

Blum, J., et al. (2025). The long way from raw data to NAM-based information: Overview on
data layers and processing steps. *ALTEX*, 42(1), 167–180.

Briggs, K., Bosc, N., Camara, T., Diaz, C., Drew, P., Drewe, W. C., Kors, J.,
van Mulligen, E., Pastor, M., Pognan, F., Quintana, J. R., Sarntivijai, S., &
Steger-Hartmann, T. (2021). Guidelines for FAIR sharing of preclinical safety and
off-target pharmacology data. *ALTEX*, 38(2), 187–197.
https://doi.org/10.14573/altex.2011181

CDISC. (n.d.). *Standard for Exchange of Nonclinical Data (SEND)*.
https://www.cdisc.org/standards/foundational/send

Deepika, D., Bharti, K., & Sharma, S. (2025). Advancing human health risk assessment: the
role of new approach methodologies. *Frontiers in Toxicology*, 7, 1632941.
https://doi.org/10.3389/ftox.2025.1632941

Kase, R., Korkaric, M., Werner, I., & Ågerstrand, M. (2016). Criteria for Reporting and
Evaluating ecotoxicity Data (CRED): comparison and perception of the Klimisch and CRED
methods for evaluating reliability and relevance of ecotoxicity studies. *Environmental
Sciences Europe*, 28, 7. https://doi.org/10.1186/s12302-016-0073-x ⚠ *verify author list*

Klimisch, H.-J., Andreae, M., & Tillmann, U. (1997). A systematic approach for evaluating
the quality of experimental toxicological and ecotoxicological data. *Regulatory Toxicology
and Pharmacology*, 25(1), 1–5. ⚠ *verify pages*

Krebs, A., et al. (2019). Template for the description of cell-based toxicological test
methods to allow evaluation and regulatory use of the data. *ALTEX*, 36(4), 682–699.
https://doi.org/10.14573/altex.1909271

Krebs, A., et al. (2020). The EU-ToxRisk method documentation, data processing and chemical
testing pipeline for the regulatory use of new approach methods. *Archives of Toxicology*,
94, 2435–2461.

Lauer, K. B., Sarntivijai, S., Blomberg, N., et al. (2022). eTRANSAFE: Building a
sustainable framework to share reproducible drug safety knowledge with the public domain.
*F1000Research*, 11, 287. PMCID: PMC9096149; PMID: 35602243. ⚠ *verify author order —
some indexes list Sarntivijai as first author*

Moermond, C. T. A., Kase, R., Korkaric, M., & Ågerstrand, M. (2016). CRED: Criteria for
reporting and evaluating ecotoxicity data. *Environmental Toxicology and Chemistry*, 35(5),
1297–1309. ⚠ *verify volume/pages*

OECD. (2017). *Guidance Document for Describing Non-Guideline In Vitro Test Methods*
(Series on Testing and Assessment No. 211). OECD Publishing.

OECD. (2018). *Guidance Document on Good In Vitro Method Practices (GIVIMP)* (Series on
Testing and Assessment No. 286). OECD Publishing.
https://doi.org/10.1787/9789264304796-en (2nd edition published 15 December 2025)

OECD. (2023). *OECD Omics Reporting Framework (OORF): Guidance on reporting elements for
the regulatory use of omics data from laboratory-based toxicology studies* (Series on
Testing and Assessment No. 390). OECD Publishing. ENV/CBC/MONO(2023)41.

OECD. (2025). *Guidance Document on the Generation, Reporting and Use of Research Data for
Regulatory Assessments* (Series on Testing and Assessment No. 417). OECD Publishing.
https://doi.org/10.1787/8d49ec1d-en — ENV/CBC/MONO(2025)18, published 3 December 2025.

OECD. (n.d.). *International Uniform ChemicaL Information Database (IUCLID)*.
https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/international-uniform-chemical-information-database.html

Ouedraogo, G., Alépée, N., Tan, B., & Roper, C. S. (2025). A call to action: Advancing new
approach methodologies (NAMs) in regulatory toxicology through a unified framework for
validation and acceptance. *Regulatory Toxicology and Pharmacology*, 162, 105904.
https://doi.org/10.1016/j.yrtph.2025.105904

Schneider, K., Schwarz, M., Burkholder, I., Kopp-Schneider, A., Edler, L.,
Kinsner-Ovaskainen, A., Hartung, T., & Hoffmann, S. (2009). "ToxRTool", a new tool to
assess the reliability of toxicological data. *Toxicology Letters*, 189(2), 138–144.
⚠ *verify author list and pages*

Soiland-Reyes, S., et al. (2022). Packaging research artefacts with RO-Crate.
*Data Science*, 5(2), 97–138.

Wilkinson, M. D., et al. (2016). The FAIR Guiding Principles for scientific data management
and stewardship. *Scientific Data*, 3, 160018.

Wittwehr, C., et al. (2024). Why adverse outcome pathways need to be FAIR. *ALTEX*, 41(1),
50–56.

⚠ **OHT 201 paper (2023).** "OECD harmonised template 201: Structuring and reporting
mechanistic information to foster the integration of new approach methodologies for hazard
and risk assessment of chemicals." *Regulatory Toxicology and Pharmacology*, 2023.
Author list and volume/page numbers could not be retrieved — please complete from Zotero.

**FDA.** *Study Data Technical Conformance Guide* / Study Data Standards Catalog — the
authoritative source for the SEND mandate dates cited in Section 3. Cite the current
version.

---

## Verification status

Every bibliographic detail above was assembled from search-result metadata. The session's
network policy blocked all direct fetching (Crossref, doi.org, PubMed Central, ScienceDirect,
OECD and Frontiers all returned 403 CONNECT denials), so **no source was opened and read in
full**. Treat the reference list as a working draft to be reconciled against Zotero.

Confirmed from multiple independent search results:

- OECD GD No. 417 (2025), DOI `10.1787/8d49ec1d-en`, ENV/CBC/MONO(2025)18, 136 pp.,
  published 3 December 2025.
- GIVIMP = OECD Series on Testing and Assessment No. 286 (2018); 2nd edition 15 December 2025.
- OORF = OECD Series on Testing and Assessment No. 390 (2023), ENV/CBC/MONO(2023)41.
- SEND mandate dates: NDA/BLA/ANDA studies starting after 17 Dec 2016; IND after
  17 Dec 2017; SENDIG v3.0 → v3.1 after 15 Mar 2019 (NDA/BLA/ANDA) and 15 Mar 2020 (IND).
- Krebs et al. (2019) ToxTemp: *ALTEX* 36(4), 682–699, DOI `10.14573/altex.1909271`.
- Briggs et al. (2021): *ALTEX* 38(2), 187–197, DOI `10.14573/altex.2011181`.
- Deepika et al. (2025): *Frontiers in Toxicology* 7:1632941, published 3 October 2025.
- Ouedraogo et al. (2025): *Regul. Toxicol. Pharmacol.* 162, 105904.
- PMC9096149 = eTRANSAFE F1000Research 2022, 11:287, PMID 35602243.
- OHT 201 scope: non-apical / intermediate effects at molecular, subcellular, cell, tissue
  and organ level from *in vitro*, *ex vivo* and *in silico* methods.

Unresolved:

- `10.1016/j.nsa.2026.106998` (and the shorter variant `…10699`). The Elsevier journal code
  `j.nsa` maps to *Neuroscience Applied*, but the article itself could not be retrieved or
  identified by search. Title/authors needed before it can be placed.
