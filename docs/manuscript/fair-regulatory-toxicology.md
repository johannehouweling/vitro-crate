# FAIR data in regulatory toxicology

Regulatory toxicology has adopted the FAIR principles to improve the transparency,
consistency and reuse of data submitted for chemical and pharmaceutical safety assessment
(Wilkinson et al., 2016), but the regulatory reading differs from the research one: a
regulatory dataset must support an independent, auditable re-derivation of a decision with
legal and economic consequences. Findability is defined against substance identity, endpoint
and legal instrument rather than a persistent identifier; accessibility is conditional,
since much evidence is claimed as confidential business information; interoperability is
imposed through mandated information models and controlled terminologies rather than
negotiated after the fact; and reusability is inseparable from documented reliability and
relevance. Submissions therefore require structured metadata, standardised formats and
traceable provenance — study identity, test facility, GLP status, protocol deviations, and
guideline and standard versions (Briggs et al., 2021; OECD, 2025). In both ecosystems below,
structured reporting became routine only once a shared information model, a validator and a
submission pipeline were made conditions of filing.

## Harmonised reporting under the OECD

OECD Harmonised Templates (OHTs) fix the fields, cardinalities and picklists for reporting
physicochemical properties, toxicity, environmental fate and ecotoxicity, so that a study
summary produced in one jurisdiction parses in another without re-interpretation. They are
implemented in IUCLID (OECD/ECHA), the submission format for REACH, biocides and plant
protection products, and disseminated through eChemPortal and ECHA's public database — the
largest openly queryable body of regulatory toxicology data in existence (OECD, n.d.).
Conformance is validated as a precondition of submission. OHT 201 ("Intermediate effects")
extends the suite to non-apical observations at molecular, subcellular, cellular, tissue and
organ level from *in vitro*, *ex vivo* and *in silico* methods, admitting NAM evidence to
the dossier structure rather than to unstructured supporting information (Wittwehr et al.,
2023); EFSA migrated OpenFoodTox 3.0 into IUCLID 6 and restructured it to the OHTs for
interoperability with the EU Common Data Platform on Chemicals (Benfenati et al., 2026).
Three limits qualify this: OHTs carry robust study summaries rather than raw or processed
data, so traceability stops at the reported result; conformance is syntactic, as many fields
remain free text; and the template bounds what can be said about test systems its authors
did not anticipate.

## Standardised nonclinical data exchange

SEND (CDISC) implements the Study Data Tabulation Model for nonclinical studies, defining
controlled terminology, standardised tabular structures and a machine-readable data
definition file, and thereby enabling automated validation, integration and cross-study
comparison (CDISC, n.d.). Unlike most FAIR initiatives in toxicology it is mandatory: the
FDA requires SEND for studies starting after 17 December 2016 (NDA, BLA, ANDA) and
17 December 2017 (IND), with SENDIG v3.0 superseded by v3.1 after 15 March 2019 and
15 March 2020 respectively, and screens submissions against technical rejection criteria
(FDA, 2024). Extensions such as SENDIG-DART broaden coverage beyond general toxicity
designs. The result — a decade of nonclinical data in one queryable structure — supports
pooled historical control databases, cross-compound comparison, automated review and
retrospective animal-to-human concordance analysis. It also delimits the NAM problem: SEND
models the conventional *in vivo* study, and does not accommodate plate-based designs,
high-dimensional omics or computational predictions.

## FAIR data as a precondition for NAM uptake

NAMs generate heterogeneous data spanning high-content imaging, transcriptomics,
metabolomics, *in vitro* assays, organ-on-chip models and computational predictions, so
their regulatory acceptance turns on standardised metadata, transparent provenance,
interoperable formats and reproducible workflows as much as on scientific validity; absent
measurable documentation there is no defensible basis for judging fitness for purpose
(Ouedraogo et al., 2025). Deepika et al. (2025) attribute the integration problem to two
causes — heterogeneity of format, structure and terminology across structured,
semi-structured and unstructured sources, and the absence of standardisation in generation
and reporting — in contrast to animal studies, whose established guidelines support
risk-assessment use, and argue ontology-based approaches as the route to machine-readable
data and models, and thus to predictive models robust enough to support IATA. The same
constraint governs machine learning on NAM evidence, which inherits its training data's
inconsistency. The loss is distributed across layers rather than concentrated at one point:
raw instrument output, processed data and interpreted information are separated by
transformations that each discard context a later assessor may need (Blum et al., 2025), so
that experimental design, exposure conditions, model characterisation, processing steps and
the regulatory question are captured inconsistently or non-machine-readably (Krebs et al.,
2020). The mechanistic scaffolding is subject to the same requirement: AOPs must themselves
be FAIR to act as an interoperability layer between assays and regulatory endpoints
(Wittwehr et al., 2024).

## An accumulating landscape of overlapping frameworks

Each gap has drawn a further reporting framework, and the frameworks now outnumber the
endpoints they were written to serve (Table 1).

**Table 1.** Principal reporting and data-structuring instruments relevant to NAM evidence,
by the object each describes.

| Instrument | Custodian | Standardises | Object | Status |
|---|---|---|---|---|
| OECD Harmonised Templates | OECD | Study summary content per endpoint | Study summary | De facto mandatory for dossiers |
| IUCLID 6 | ECHA / OECD | Dossier structure, submission format | Substance dossier | Mandatory (REACH, biocides, PPP) |
| OHT 201 "Intermediate effects" | OECD | Non-apical, mechanistic observations | Observation | Available; adoption emerging |
| CDISC SEND / SENDIG | CDISC | Nonclinical study tabulation | *In vivo* study | Mandatory (US FDA) |
| GD 34 | OECD | Validation and regulatory acceptance | Method | Guidance |
| GD 211 | OECD | Non-guideline *in vitro* method description | Method | Guidance |
| ToxTemp | Academic community | Operationalisation of GD 211 | Method | Voluntary |
| GIVIMP (GD 286) | OECD | *In vitro* method quality practices | Method, facility | Guidance |
| OORF (No. 390) | OECD | Transcriptomics / metabolomics reporting | Dataset | Guidance |
| QMRF / QPRF | JRC / ECHA | (Q)SAR model and prediction reporting | Model, prediction | Expected with QSAR evidence |
| EFSA FAIR statement | EFSA | FAIR interpretation for effect models | Model | Statement |
| GD 417 | OECD | Generation, reporting, use of research data | Research dataset | Guidance |
| ISA, Bioschemas, RO-Crate | Community | Structure and packaging | Research object | Community standard |

Three properties make the landscape harder than the sum of its parts. The instruments
describe *different objects* — method, facility, study summary, dataset, model, prediction,
dossier — with no maintained crosswalks, so conformance with one implies nothing about
another and the same experiment is re-described in several vocabularies. They differ in
legal standing, from mandatory format through guidance to voluntary convention, giving the
producer no signal as to which subset suffices for a given regulatory question. And the
vocabulary of harmonisation is itself unharmonised: EFSA glosses the final FAIR principle as
reproducibility rather than reusability (EFSA, 2025).

The burden falls on the party least equipped to carry it. A laboratory generating a NAM
dataset for eventual regulatory use may face a funder's data-management requirement, a
journal data policy, GD 211 and ToxTemp for the method, GIVIMP for quality practice, the
OORF for transcriptomics and OHT 201 for dossier entry — each with its own template,
terminology and granularity, none reading another's output. A single ToxTemp has been
estimated at up to five days (ONTOX, 2025); the aggregate predicts its own outcome in
partial and inconsistent compliance. The pattern is not redundancy but an unresolved
division of labour: each framework specifies what must be recorded about one object, none
specifies a machine-actionable container in which records and data travel together, and the
integration work is repeated by every producer and every assessor. The productive signals
are consolidative — OpenFoodTox into IUCLID/OHT (Benfenati et al., 2026), mechanistic data
into OHT 201 (Wittwehr et al., 2023), legacy studies into SEND (Lauer et al., 2022) — each
reusing an existing carrier. What is required is therefore not a further reporting standard
but a packaging convention able to *carry* multiple conformance claims, binding an explicit
declaration of the standards a dataset adheres to to the data and method descriptions those
standards qualify.

## Federated infrastructure for proprietary evidence

eTRANSAFE extended FAIR implementation beyond regulatory reporting through a federated
Knowledge Hub integrating public and proprietary preclinical and clinical safety data via
ontology services, identifier management, semantic integration and computational workflows
(Lauer et al., 2022). Federation is the substantive choice: identifiers and semantics are
harmonised so that analyses execute across institutional boundaries while competitively
sensitive data remain under their owners' control — a working answer to the conditional
accessibility described above, and transferable to NAM data from industry–academia
consortia. The project also issued FAIR data-sharing, research reproducibility and model
verification guidelines, compiled from regulator, industry and academic feedback and written
as generic reusable deliverables (Briggs et al., 2021), and converted legacy toxicology
studies into CDISC SEND, demonstrating that FAIRification can be retrofitted — at a cost
prospective standardisation avoids.

## Academic and other non-standard data as regulatory evidence

A large body of academic data exists outside GLP and OECD Test Guidelines that nonetheless
carries mechanistic, hazard or exposure information relevant to regulatory decisions, and
for endpoints poorly served by guideline studies, such as developmental neurotoxicity, may
be the only evidence available. Funder FAIR mandates and journal data policies are making it
progressively more accessible — but accessibility is not the binding constraint. Reporting
completeness is: reliability and relevance cannot be judged when the test system is
under-characterised, test substance identity and purity are unstated, concentrations are
nominal and unverified, or the statistical treatment cannot be reconstructed. OECD Guidance
Document 417 addresses this directly, structuring recommendations around the research-data
lifecycle — generation, reporting, identification, evaluation, integration — and assigning
them to distinct actors: funders, researchers, publishers, repository managers, assessors
and risk managers (OECD, 2025). Its significance is that it places obligations at the point
of generation rather than asking assessors to repair under-documented studies downstream.
For data already in hand, structured appraisal instruments bridge availability and use: the
Klimisch categories (Klimisch et al., 1997), operationalised and extended by ToxRTool
(Schneider et al., 2009), CRED (Moermond et al., 2016) and SciRAP (Beronius et al., 2018),
each separating reliability from relevance and making the basis of judgement explicit. These
are complementary to FAIR, not substitutes for it: FAIRness determines whether a study can
be located and interrogated at acceptable cost, these instruments whether it can be relied
upon. A perfectly FAIR dataset can still be regulatorily unusable.

## FAIR methods and FAIR method-derived data

Making a NAM FAIR and making NAM-derived data FAIR are distinct problems, frequently
conflated, with different infrastructure consequences. Making the method FAIR — the assay,
model or protocol — means describing it well enough to be assessed, reproduced and
transferred independently of any dataset it produced. ToxTemp (Krebs et al., 2019) does this
by decomposing OECD Guidance Document 211 (OECD, 2017) into concrete questions on
test-system characterisation, procedural detail and explicit acceptance criteria; GIVIMP
supplies the quality-practice counterpart (OECD, 2018); DB-ALM and TSAR supply method-level
findability. The unit is the method, and its persistent identity is what allows
independently generated datasets to be recognised as products of the same procedure. That
this is general rather than *in vitro*-specific is shown by EFSA's parallel treatment of
mechanistic effect models, which interprets each FAIR principle for models used in the
environmental risk assessment of pesticides and argues that FAIRer models would yield a more
efficient review process and better integration of model-based evidence (EFSA, 2025): a
model, like an assay, is a method, and the object requiring FAIRification is the method and
not only the numbers it emits.

Making NAM-derived data FAIR concerns the individual experiment — which substance, which
biological model, which exposure design, which endpoint, which pipeline, which regulatory
question — and its infrastructure is ontological and structural rather than narrative:
persistent identifiers and controlled vocabularies for chemicals, cell lines, assays and
endpoints, minimum information models per data type, and a packaging convention keeping raw
data, processed data, scripts, protocols and metadata together as one machine-actionable
research object (Soiland-Reyes et al., 2022). A third object completes the set: a
well-described method with no linked data cannot be evaluated on evidence, well-packaged
data pointing at an under-described method cannot be evaluated at all, and both remain
irreproducible unless the analytical workflow that produced the reported result is captured
and re-executable. Existing infrastructures cover these unevenly — IUCLID and the OHTs the
reported result, SEND the study tabulation, GD 211 and ToxTemp the method description, the
OORF omics reporting, EFSA's statement the model — but none binds method description, raw
and processed data, analysis code and regulatory context into one traceable,
machine-actionable unit.

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

EFSA. (2025). Statement on the interpretation of FAIR principles for mechanistic effect
models in the regulatory environmental risk assessment of pesticides. *EFSA Journal*,
23(11), e9741. https://doi.org/10.2903/j.efsa.2025.9741

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

OECD. (2017). *Guidance Document for Describing Non-Guideline In Vitro Test Methods*
(Series on Testing and Assessment No. 211).

OECD. (2018). *Guidance Document on Good In Vitro Method Practices (GIVIMP)* (Series on
Testing and Assessment No. 286). https://doi.org/10.1787/9789264304796-en

OECD. (2023). *OECD Omics Reporting Framework (OORF)* (Series on Testing and Assessment
No. 390).

OECD. (2025). *Guidance Document on the Generation, Reporting and Use of Research Data for
Regulatory Assessments* (Series on Testing and Assessment No. 417).
https://doi.org/10.1787/8d49ec1d-en

OECD. (n.d.). *International Uniform ChemicaL Information Database (IUCLID)*.
https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/international-uniform-chemical-information-database.html

ONTOX. (2025). [ToxTemp completion-effort estimate — complete from project deliverable.]

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
