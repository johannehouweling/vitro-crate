# FAIR data in regulatory toxicology

Regulatory toxicology has increasingly adopted the FAIR principles to improve the
transparency, consistency and reuse of data submitted for chemical and pharmaceutical
safety assessment (Wilkinson et al., 2016). The framing, however, differs from that used
for research data. A research dataset is FAIR when a competent peer can find it, obtain it,
combine it with other data and build on it. A regulatory dataset must additionally support
an independent, auditable re-derivation of a conclusion that carries legal and economic
consequences. Each facet is reinterpreted accordingly: findability is defined against
substance identity, endpoint and the legal instrument under which a study was submitted
rather than by deposition with a persistent identifier; accessibility is conditional rather
than open, since much of the underlying evidence is claimed as confidential business
information and is therefore disclosed to defined actors under defined conditions;
interoperability is achieved by construction, through mandated information models and
controlled terminologies, rather than negotiated after the fact; and reusability is
inseparable from documented reliability and relevance, because a dataset whose test system,
exposure design and acceptance criteria cannot be reconstructed is unusable for its intended
purpose however well it scores against generic FAIR metrics. Regulatory submissions
therefore require highly structured metadata, standardised reporting formats and traceable
provenance — including study identity, test facility, GLP status, protocol deviations and
the versions of the guideline and reporting standard applied — to enable independent
evaluation and regulatory decision-making (Briggs et al., 2021; OECD, 2025). It is worth
noting how this machine-actionability was obtained: in the regulatory ecosystems described
below, structured reporting became routine only once a shared information model, a
validation service and a submission pipeline were made a condition of filing.

## Harmonised reporting under the OECD

The Organisation for Economic Co-operation and Development (OECD) has played a central role
through the development of the OECD Harmonised Templates (OHTs), which provide structured,
machine-readable templates for reporting physicochemical properties, toxicity studies,
environmental fate and ecotoxicity data. Each template fixes the fields, cardinalities and
picklists for a given endpoint, so that a study summary produced in one jurisdiction can be
parsed and evaluated in another without re-keying or re-interpretation. These templates are
implemented in IUCLID (International Uniform ChemicaL Information Database), maintained
jointly by the OECD and the European Chemicals Agency, which serves as the submission format
for REACH registrations and is also used for biocides, plant protection products and by
regulatory programmes outside the European Union (OECD, n.d.). The resulting records are
surfaced across jurisdictions through eChemPortal and, for REACH, through ECHA's public
dissemination platform, which together constitute the largest openly queryable body of
regulatory toxicology data in existence. The gravitational pull of this ecosystem is
illustrated by its adoption beyond its original scope: EFSA's chemical hazards database
OpenFoodTox was migrated in its third major version into IUCLID 6 and restructured in line
with the OHTs, explicitly to enable standardised collection and reporting and
interoperability with the EU Common Data Platform on Chemicals (Benfenati et al., 2026).

Two features make this ecosystem instructive as a FAIR implementation. First,
interoperability is a precondition of submission rather than an aspiration: dossiers are
validated against the template specification before they are accepted. Second, the OHT suite
has been deliberately extended to accommodate mechanistic and non-apical evidence. OHT 201
("Intermediate effects") was introduced to allow reporting of observations at the molecular,
subcellular, cellular, tissue and organ level obtained from *in vitro*, *ex vivo* and
*in silico* methods, so that NAM-derived evidence can enter the same dossier structure as
guideline study summaries rather than remaining as unstructured supporting information
(Wittwehr et al., 2023). The limitations are equally instructive. OHTs capture robust study
summaries rather than raw or processed data, so the traceability chain typically terminates
at the reported result; template conformance guarantees syntactic but not full semantic
interoperability, since many fields remain free text; and for any test system the template's
authors did not anticipate, the expressive power of the template constrains what can be
said.

## Standardised nonclinical data exchange

The Standard for Exchange of Nonclinical Data (SEND), developed by the Clinical Data
Interchange Standards Consortium (CDISC), standardises the organisation and exchange of
non-clinical toxicology studies submitted to regulatory agencies (CDISC, n.d.). SEND is an
implementation of the CDISC Study Data Tabulation Model for nonclinical work: it defines
common terminology, controlled vocabularies and standardised tabular data structures,
together with a machine-readable data definition file, enabling automated validation, data
integration and cross-study comparison while supporting long-term reuse of regulatory
datasets. Unlike most FAIR initiatives in toxicology, SEND is mandatory. The United States
Food and Drug Administration requires SEND datasets for nonclinical studies starting after
17 December 2016 for NDA, BLA and ANDA submissions and after 17 December 2017 for INDs, with
the applicable version of the SEND Implementation Guide stepping from v3.0 to v3.1 for
studies beginning after 15 March 2019 and 15 March 2020 respectively (FDA, 2024).
Submissions are screened against technical rejection criteria, so a non-conformant dataset
is returned rather than merely discouraged, and domain-specific extensions such as
SENDIG-DART broaden coverage beyond general toxicity study designs.

The practical consequence is that a decade of nonclinical safety data now exists in a
single, queryable structure, enabling uses that were previously impractical: pooled
historical control databases, systematic cross-study and cross-compound comparison,
automated reviewer workflows, and retrospective analysis of concordance between animal
findings and human outcomes. It also delimits the challenge for NAMs. SEND models the
conventional *in vivo* study — subjects, groups and findings over time — and does not
natively accommodate plate-based *in vitro* designs, high-dimensional omics readouts or
computational predictions. The lesson transfers even though the schema does not.

## FAIR data as a precondition for NAM uptake

The increasing adoption of new approach methodologies (NAMs) for regulatory decision-making
has highlighted the importance of FAIR data to ensure that diverse evidence streams can be
interpreted, integrated and reused throughout chemical safety assessment. Unlike traditional
animal studies, NAMs generate heterogeneous datasets spanning high-content imaging,
transcriptomics, metabolomics, *in vitro* assays, organ-on-chip models and computational
predictions. Their regulatory acceptance therefore depends not only on scientific validity
but also on standardised metadata, transparent provenance, interoperable data formats and
reproducible analytical workflows. Recent calls for a unified validation and acceptance
framework make the same point from the quality-assurance side: absent measurable,
standardised documentation there is no defensible basis on which to judge whether a method
is fit for a stated regulatory purpose (Ouedraogo et al., 2025).

Deepika et al. (2025) present FAIRification and harmonisation as enablers of NAM data
usability, and thus of NAM applicability. NAM data are now generated in rapidly growing
volumes, but integrating and using them remains difficult. The authors attribute this to two
problems: the data are heterogeneous in format, structure and terminology across structured,
semi-structured and unstructured sources; and their generation and reporting lack
standardisation. This contrasts with animal studies, whose established guidelines support
their use in risk assessment. Ontology-based approaches are argued as a route to
machine-readable data and models, and thereby to the more reproducible and robust predictive
models that NAMs need in order to support Integrated Approaches to Testing and Assessment
(IATA) — a route that also conditions any credible use of machine learning on NAM evidence,
since such methods inherit whatever inconsistency is present in their training data.

The obstacle is better understood as several bottlenecks distributed across the layers
through which NAM evidence passes than as a single one. Blum et al. (2025) describe the
route from raw instrument output through processed data to interpreted, decision-relevant
information as a sequence of transformations, each of which discards context that a later
assessor may need. In practice, experimental design, exposure conditions, biological model
characterisation, data-processing steps and the regulatory question being addressed are
captured inconsistently, dispersed across documents, or held only in non-machine-readable
form (Krebs et al., 2020; Blum et al., 2025). The same argument has been made for the
mechanistic scaffolding on which NAM evidence is hung: adverse outcome pathways must
themselves be FAIR if they are to serve as a stable interoperability layer between assays
and regulatory endpoints (Wittwehr et al., 2024).

## An accumulating landscape of overlapping frameworks

The regulatory response to each of these gaps has been a further reporting framework, and
the frameworks now substantially outnumber the endpoints they were written to serve. GIVIMP
(OECD, 2018) sets out good *in vitro* method practices covering test systems, reagents,
standard operating procedures, method performance and record retention. The OECD Omics
Reporting Framework (OECD, 2023) defines the reporting elements required for regulatory use
of transcriptomics and metabolomics data. For computational methods the (Q)SAR Model
Reporting Format and Prediction Reporting Format play the analogous role, and EFSA has
issued its own interpretation of FAIR for mechanistic effect models (EFSA, 2025). Guidance
Documents 34 and 211 govern validation and non-guideline method description respectively
(OECD, 2005, 2017), ToxTemp operationalises the latter (Krebs et al., 2019), and Guidance
Document 417 addresses research data generated outside regulatory frameworks altogether
(OECD, 2025). Table 1 summarises the principal instruments a NAM dataset may be expected to
satisfy.

**Table 1.** Principal reporting and data-structuring instruments relevant to NAM evidence
in a regulatory context, by the object each describes.

| Instrument | Custodian | What it standardises | Object described | Status |
|---|---|---|---|---|
| OECD Harmonised Templates | OECD | Study summary content per endpoint | Study summary | De facto mandatory for dossier submission |
| IUCLID 6 | ECHA / OECD | Dossier structure and submission format | Substance dossier | Mandatory (REACH, biocides, PPP) |
| OHT 201 "Intermediate effects" | OECD | Non-apical and mechanistic observations | Observation | Available; adoption emerging |
| CDISC SEND / SENDIG | CDISC | Nonclinical study tabulation | *In vivo* study | Mandatory (US FDA) |
| GD 34 | OECD | Validation and regulatory acceptance | Method | Guidance |
| GD 211 | OECD | Non-guideline *in vitro* method description | Method | Guidance |
| ToxTemp | Academic community | Operationalisation of GD 211 | Method | Voluntary |
| GIVIMP (GD 286) | OECD | *In vitro* method quality practices | Method and facility | Guidance |
| OECD Omics Reporting Framework (No. 390) | OECD | Transcriptomics / metabolomics reporting | Dataset | Guidance |
| QMRF / QPRF | JRC / ECHA | (Q)SAR model and prediction reporting | Model and prediction | Expected with QSAR evidence |
| EFSA FAIR statement | EFSA | FAIR interpretation for effect models | Model | Statement |
| GD 417 | OECD | Generation, reporting and use of research data | Research dataset | Guidance |
| ISA, Bioschemas, RO-Crate | Community | Structure and packaging | Research object | Community standard |

Three properties of this landscape create difficulty beyond the sum of its parts. First, the
instruments describe *different objects* — a method, a facility, a study summary, a dataset,
a model, a prediction, a dossier — and there are no maintained crosswalks between them, so
conformance with one implies nothing about conformance with another and the same underlying
experiment must be re-described in several vocabularies. Second, they differ in legal
standing, from mandatory submission formats through guidance to voluntary community
conventions, which means a data producer cannot infer priority from the documents themselves
and receives no signal about which subset is sufficient for a given regulatory question.
Third, even the vocabulary of harmonisation is not harmonised: EFSA's statement glosses the
final FAIR principle as reproducibility rather than reusability (EFSA, 2025), a small
divergence that nonetheless illustrates how a principle can be locally reinterpreted at each
point of adoption.

The burden of reconciling this falls on the party least equipped to carry it. A single
academic laboratory generating a NAM dataset intended for eventual regulatory use may be
expected to satisfy a funder's FAIR data-management requirement, a journal's data
availability policy, GD 211 and ToxTemp for the method description, GIVIMP for quality
practices, the Omics Reporting Framework if transcriptomics are involved, and OHT 201 should
the data enter a dossier — each with its own template, terminology and level of granularity,
and none of which reads the others' output. Completing a single ToxTemp has been estimated
to take up to five days (ONTOX, 2025); the aggregate is a documentation burden that predicts
its own outcome, namely partial and inconsistent compliance. What the proliferation reflects
is not redundancy so much as an unresolved division of labour: each framework specifies what
must be recorded about one object, none specifies a machine-actionable container in which
the records and the data travel together, and consequently the integration work is repeated
by every producer and every assessor.

The more promising signals are therefore consolidative rather than additive. The migration
of OpenFoodTox into IUCLID 6 and the OHTs (Benfenati et al., 2026), the extension of the OHT
suite to mechanistic evidence through OHT 201 (Wittwehr et al., 2023), and eTRANSAFE's
retrofitting of legacy studies into CDISC SEND (Lauer et al., 2022) all reuse an existing
carrier rather than minting a new one. This suggests that the practical requirement is not a
further reporting standard but a packaging convention capable of *carrying* multiple
conformance claims — declaring explicitly which guidance and community standards a given
dataset adheres to, and binding those declarations to the data and method descriptions they
qualify.

## Federated infrastructure for proprietary evidence

Projects such as eTRANSAFE have extended FAIR implementation beyond regulatory reporting by
developing an interoperable knowledge infrastructure for translational drug safety
assessment. The project established a federated Knowledge Hub integrating public and
proprietary preclinical and clinical safety data using ontology services, identifier
management, semantic integration and computational workflows (Lauer et al., 2022). The
federated architecture is the substantive design choice: rather than requiring
pharmaceutical partners to pool competitively sensitive study data centrally, the
infrastructure harmonises identifiers and semantics so that analyses can be executed across
institutional boundaries while the data remain under their owners' control. This is a
concrete answer to the accessibility tension described above, and a transferable one for NAM
data generated within industry–academia consortia. In addition to the infrastructure,
eTRANSAFE produced FAIR data-sharing guidelines, research reproducibility guidelines and
model verification guidelines to support consistent stewardship of regulatory toxicology
data (Briggs et al., 2021; Lauer et al., 2022). These were compiled from feedback across
regulatory agencies, industry and academic partners and written as generic, reusable
deliverables rather than project-internal documentation. The project also converted legacy
toxicology studies into CDISC SEND, retrofitting a standard onto historical data to
facilitate harmonised exchange across pharmaceutical partners and regulatory stakeholders —
a demonstration that FAIRification is not restricted to prospectively generated data, albeit
at a cost that prospective standardisation avoids.

## Academic and other non-standard data as regulatory evidence

Besides formal submissions and NAM databases, a large body of academic data exists. Such
data are typically not generated under Good Laboratory Practice or to a specific OECD Test
Guideline, but may carry mechanistic, hazard or exposure information valuable for regulatory
decision-making — and for endpoints that guideline studies address poorly, such as
developmental neurotoxicity, they may be the only available evidence. Because public funding
policies now generally require adherence to the FAIR principles, and because journals and
funders increasingly mandate data availability statements and deposition, such academic and
other non-standard data are expected to become progressively more accessible and reusable.

Accessibility, however, is not the binding constraint. The obstacle to regulatory use of
academic data has consistently been reporting completeness: an assessor cannot judge
reliability and relevance when the test system is under-characterised, the identity and
purity of the test substance are unstated, concentrations are nominal and unverified, or the
statistical treatment cannot be reconstructed. This is addressed directly by the OECD
*Guidance Document on the Generation, Reporting and Use of Research Data for Regulatory
Assessments* (OECD, 2025), which is structured around the lifecycle of research data — from
generation and reporting through identification, evaluation and integration into regulatory
assessments — and issues practical recommendations to distinct actors: funders, researchers,
publishers, repository managers, assessors and risk managers. Its significance lies in
placing obligations upstream, at the point of data generation, rather than asking assessors
to repair under-documented studies downstream. Where such data are already in hand,
structured appraisal instruments bridge availability and use. The Klimisch categories
(Klimisch et al., 1997) remain the reference point and have been operationalised and extended
by tools including ToxRTool (Schneider et al., 2009), the CRED criteria for ecotoxicity data
(Moermond et al., 2016) and the SciRAP platform (Beronius et al., 2018), each of which
separates reliability from relevance and makes the basis of the judgement explicit. Their
relationship to FAIR is complementary rather than substitutive: FAIRness determines whether
a study can be located and interrogated at acceptable cost, while these instruments determine
whether it can be relied upon. A perfectly FAIR dataset can still be regulatorily unusable,
and the distinction is worth preserving in any claim that FAIRification advances regulatory
uptake.

## FAIR methods and FAIR method-derived data

A distinction frequently collapsed, but consequential for both infrastructure design and
evaluation, is that between making a NAM FAIR and making NAM-derived data FAIR. Making the
NAM itself — the assay, model or protocol — FAIR means describing the method in sufficient
detail that it can be assessed, reproduced and transferred independently of any particular
dataset it has produced. Structured templates such as ToxTemp (Krebs et al., 2019) were
developed to satisfy OECD Guidance Document 211 on describing non-guideline *in vitro* test
methods (OECD, 2017) by decomposing its requirements into concrete questions covering
test-system characterisation, procedural detail and explicit acceptance criteria. GIVIMP
supplies the quality-practice counterpart (OECD, 2018), and method-level findability is
served by registries such as the EURL ECVAM DataBase service on ALternative Methods and the
Tracking System for Alternative methods towards Regulatory acceptance. The unit of
description is the method, and its persistent identity is what allows independently
generated datasets to be recognised as products of the same procedure.

That this is a general regulatory problem rather than an *in vitro* one is shown by EFSA's
parallel treatment of mechanistic effect models. Its working group on effect models in
environmental risk assessment set out an interpretation of each FAIR principle specifically
for models used in the regulatory environmental risk assessment of pesticides, surveying the
resources available to support implementation and arguing that greater FAIRness of the
models themselves would yield a more efficient review process and better integration of
model-based evidence, with benefits across stakeholder groups (EFSA, 2025). The statement is
deliberately non-prescriptive, intended to open discussion within the modelling community
rather than impose a template. Its significance here is structural: a model, like an assay,
is a *method*, and EFSA's argument is that the object requiring FAIRification is the method
and not only the numbers it emits.

Making NAM-derived data FAIR is a different problem, concerned with the individual
experiment: which substance was tested, on which biological model, under which exposure
design, measuring which endpoint, processed by which pipeline, and answering which
regulatory question. Here the enabling infrastructure is ontological and structural rather
than narrative — persistent identifiers and controlled vocabularies for chemicals, cell
lines, assays and endpoints, minimum information models for the relevant data type, and a
packaging convention that keeps raw data, processed data, scripts, protocols and metadata
together as one machine-actionable research object rather than as a folder of files
(Soiland-Reyes et al., 2022). The two are mutually dependent, and a third object completes
the set: a well-described method with no linked data cannot be evaluated on evidence,
well-packaged data pointing at an under-described method cannot be evaluated at all, and
both remain irreproducible unless the analytical workflow that turned measurements into the
reported result is itself captured and re-executable. Existing regulatory infrastructures
address these unevenly. IUCLID and the OHTs standardise the reported result, SEND
standardises the study tabulation, GD 211 and ToxTemp standardise the method description,
the Omics Reporting Framework standardises omics reporting, and EFSA's statement addresses
the model; but no single instrument currently binds method description, raw and processed
data, analysis code and regulatory context into one traceable, machine-actionable unit.

---

## References

Benfenati, E., et al. (2026). Further development and update of EFSA's Chemical Hazards
database: OpenFoodTox 3.0. *EFSA Supporting Publications*, EN-10099.
https://doi.org/10.2903/sp.efsa.2026.EN-10099

Beronius, A., Molander, L., Zilliacus, J., Rudén, C., & Hanberg, A. (2018). Testing and
refining the Science in Risk Assessment and Policy (SciRAP) web-based platform for
evaluating the reliability and relevance of *in vivo* toxicity studies. *Journal of Applied
Toxicology*, 38(12), 1460–1470.

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

EFSA (European Food Safety Authority). (2025). Statement on the interpretation of FAIR
principles for mechanistic effect models in the regulatory environmental risk assessment of
pesticides. *EFSA Journal*, 23(11), e9741. https://doi.org/10.2903/j.efsa.2025.9741

FDA. (2024). *Study Data Technical Conformance Guide*. U.S. Food and Drug Administration.

Klimisch, H.-J., Andreae, M., & Tillmann, U. (1997). A systematic approach for evaluating
the quality of experimental toxicological and ecotoxicological data. *Regulatory Toxicology
and Pharmacology*, 25(1), 1–5.

Krebs, A., et al. (2019). Template for the description of cell-based toxicological test
methods to allow evaluation and regulatory use of the data. *ALTEX*, 36(4), 682–699.
https://doi.org/10.14573/altex.1909271

Krebs, A., et al. (2020). The EU-ToxRisk method documentation, data processing and chemical
testing pipeline for the regulatory use of new approach methods. *Archives of Toxicology*,
94, 2435–2461.

Lauer, K. B., Sarntivijai, S., Blomberg, N., et al. (2022). eTRANSAFE: Building a
sustainable framework to share reproducible drug safety knowledge with the public domain.
*F1000Research*, 11, 287.

Moermond, C. T. A., Kase, R., Korkaric, M., & Ågerstrand, M. (2016). CRED: Criteria for
reporting and evaluating ecotoxicity data. *Environmental Toxicology and Chemistry*, 35(5),
1297–1309.

OECD. (2005). *Guidance Document on the Validation and International Acceptance of New or
Updated Test Methods for Hazard Assessment* (Series on Testing and Assessment No. 34).
OECD Publishing.

OECD. (2017). *Guidance Document for Describing Non-Guideline In Vitro Test Methods*
(Series on Testing and Assessment No. 211). OECD Publishing.

OECD. (2018). *Guidance Document on Good In Vitro Method Practices (GIVIMP)* (Series on
Testing and Assessment No. 286). OECD Publishing.
https://doi.org/10.1787/9789264304796-en

OECD. (2023). *OECD Omics Reporting Framework (OORF): Guidance on reporting elements for
the regulatory use of omics data from laboratory-based toxicology studies* (Series on
Testing and Assessment No. 390). OECD Publishing.

OECD. (2025). *Guidance Document on the Generation, Reporting and Use of Research Data for
Regulatory Assessments* (Series on Testing and Assessment No. 417). OECD Publishing.
https://doi.org/10.1787/8d49ec1d-en

OECD. (n.d.). *International Uniform ChemicaL Information Database (IUCLID)*.
https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/international-uniform-chemical-information-database.html

ONTOX. (2025). [Estimate of ToxTemp completion effort — complete citation from project
deliverable.]

Ouedraogo, G., Alépée, N., Tan, B., & Roper, C. S. (2025). A call to action: Advancing new
approach methodologies (NAMs) in regulatory toxicology through a unified framework for
validation and acceptance. *Regulatory Toxicology and Pharmacology*, 162, 105904.
https://doi.org/10.1016/j.yrtph.2025.105904

Pineda-Pampliega, J., et al. (2022). Developing a framework for open and FAIR data
management practices for next generation risk- and benefit assessment of fish and seafood.
*EFSA Journal*, 20(S1), e200917. https://doi.org/10.2903/j.efsa.2022.e200917

Schneider, K., Schwarz, M., Burkholder, I., Kopp-Schneider, A., Edler, L.,
Kinsner-Ovaskainen, A., Hartung, T., & Hoffmann, S. (2009). "ToxRTool", a new tool to
assess the reliability of toxicological data. *Toxicology Letters*, 189(2), 138–144.

Soiland-Reyes, S., et al. (2022). Packaging research artefacts with RO-Crate.
*Data Science*, 5(2), 97–138.

Wilkinson, M. D., et al. (2016). The FAIR Guiding Principles for scientific data management
and stewardship. *Scientific Data*, 3, 160018.

Wittwehr, C., et al. (2023). OECD harmonised template 201: Structuring and reporting
mechanistic information to foster the integration of new approach methodologies for hazard
and risk assessment of chemicals. *Regulatory Toxicology and Pharmacology*.

Wittwehr, C., et al. (2024). Why adverse outcome pathways need to be FAIR. *ALTEX*, 41(1),
50–56.
