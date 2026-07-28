# FAIR data in regulatory toxicology

Regulatory toxicology has adopted the FAIR principles to improve the transparency,
consistency and reuse of data submitted for chemical and pharmaceutical safety assessment
(Wilkinson et al., 2016), but the regulatory reading differs from the research one: a
regulatory dataset must support an independent, auditable re-derivation of a decision with
legal and economic consequences, frequently in a jurisdiction other than the one that
generated it. Findability is defined against substance identity, endpoint and legal
instrument rather than a persistent identifier; accessibility is conditional, since much
evidence is claimed as confidential business information; interoperability is imposed
through mandated information models and controlled terminologies rather than negotiated
after the fact; and reusability is inseparable from documented reliability and relevance.
Submissions therefore require structured metadata, standardised formats and traceable
provenance — study identity, test facility, GLP status, protocol deviations, and guideline
and standard versions (Briggs et al., 2021; OECD, 2025b).

## An international system by construction

Regulatory toxicology is a worldwide enterprise governed by binding international agreement,
and this is the reason harmonised reporting exists at all. Under the OECD Decision on the
Mutual Acceptance of Data (MAD), adopted by the Council on 12 May 1981, data generated in
the testing of chemicals in one adhering country in accordance with OECD Test Guidelines and
the OECD Principles of Good Laboratory Practice must be accepted by all others for purposes
of health and environmental assessment (OECD, 1981). MAD is a Council Decision and therefore
legally binding on adherents; it covers 45 countries — the 38 OECD members plus seven
non-member adherents (Argentina, Brazil, India, Malaysia, Singapore, South Africa and
Thailand) — and has been open to non-members since 1997, on a two-stage ladder in which a
provisional adherent accepts others' data and attains reciprocity once its national GLP
compliance monitoring programme has been evaluated by the OECD. Several jurisdictions with
their own domestic chemicals frameworks — Japan's CSCL, Korea's K-REACH, China's MEE Order
No. 12, Canada's modernised CEPA, Brazil's Law 15,022/2024, Australia's AICIS — therefore
operate inside, or alongside, a shared data-acceptance system rather than in isolation. The
practical corollary is that a study summary is a cross-border object: interoperability is
not an efficiency but a condition of the agreement, and the OECD Test Guidelines Programme
(some 150 methods) and the Harmonised Templates are its methodological and data-layer
expressions.

Three further instruments extend the same logic to NAMs. The International Cooperation on
Alternative Test Methods (ICATM), established by memorandum in 2009 among the validation
bodies of Canada, the European Union, Japan and the United States and joined by Korea in
2011, coordinates validation studies and peer review expressly to secure worldwide
acceptance of alternative methods; its position paper on defined approaches for skin
sensitisation led directly to an OECD test guideline (Casati et al., 2018). Accelerating the
Pace of Chemical Risk Assessment (APCRA), a government-to-government initiative founded in
2016 spanning North American, European and Asia-Pacific agencies, runs collaborative case
studies aimed at international acceptance of NAM-derived metrics (Kavlock et al., 2018). In
the pharmaceutical sector, ICH — founded in 1990 by regulators and industry from Europe,
Japan and the United States, and since its 2015 reconstitution comprising regulatory members
from Latin America, the Middle East and East and Southeast Asia — harmonises nonclinical
safety testing through its S-series guidelines, and the International Medicines Regulators
Working Group on 3Rs, established in 2024 by EMA, FDA, PMDA, Health Canada, TGA and
Swissmedic, was created to reach internationally harmonised 3Rs recommendations including
acceptance criteria for NAMs. NAM outputs have also entered worldwide hazard communication:
the eleventh revised edition of the UN Globally Harmonised System, published in 2025,
introduces guidance for classifying skin sensitisation using non-animal methods (UNECE,
2025).

## Harmonised reporting under the OECD

The OECD Harmonised Templates (OHTs), developed since 2002 and now numbering around 130, fix
the fields, cardinalities and picklists for reporting physicochemical properties, toxicity,
environmental fate and ecotoxicity, and share XML schemas so that records can be exchanged
between systems that are otherwise unrelated. They are implemented in IUCLID, maintained by
ECHA with the OECD: the submission format for REACH, biocides and plant protection products,
mandatory for assessment certificate applications to Australia's AICIS, and used by the OECD
for its own cooperative chemicals assessment work. Records surface internationally through
eChemPortal, an OECD portal hosted by ECHA that federates ECHA's REACH dissemination
database, OECD SIDS, Japan's J-CHECK and US EPA resources among others, and indexed over 1.6
million substances by January 2026. Conformance is validated as a precondition of
submission. OHT 201 ("Intermediate effects") extends the suite to non-apical observations at
molecular, subcellular, cellular, tissue and organ level obtained from *in vitro*, *ex vivo*
and *in silico* methods, admitting NAM evidence to the dossier structure rather than to
unstructured supporting information (Carnesecchi et al., 2023); EFSA migrated OpenFoodTox
into IUCLID 6 and restructured it to the OHTs for interoperability with the EU Common Data
Platform on Chemicals (Benfenati et al., 2026). Three limits qualify this: OHTs carry robust
study summaries rather than raw or processed data, so traceability stops at the reported
result; conformance is syntactic, as many fields remain free text; and the template bounds
what can be said about test systems its authors did not anticipate.

## Standardised nonclinical data exchange

SEND (CDISC) implements the Study Data Tabulation Model for nonclinical studies, defining
controlled terminology, standardised tabular structures and a machine-readable data
definition file, and thereby enabling automated validation, integration and cross-study
comparison (CDISC, n.d.). Unlike most FAIR initiatives in toxicology it is mandatory: the
FDA requires SEND for studies starting after 17 December 2016 (NDA, BLA, ANDA) and
17 December 2017 (IND), with SENDIG v3.0 superseded by v3.1 after 15 March 2019 and
15 March 2020 respectively, and screens submissions against technical rejection criteria
(FDA, 2024). Its international position differs from that of the OECD templates: other ICH
regulators, notably Japan's PMDA and China's CDE, have adopted the CDISC clinical standards
SDTM and ADaM but are at varying stages with respect to nonclinical data, so SEND
harmonises a filing pathway rather than a treaty obligation. The result — a decade of
nonclinical data in one queryable structure — supports pooled historical control databases,
cross-compound comparison, automated review and retrospective animal-to-human concordance
analysis. It also delimits the NAM problem: SEND models the conventional *in vivo* study,
and does not accommodate plate-based designs, high-dimensional omics or computational
predictions.

## FAIR data as a precondition for NAM uptake

The increasing adoption of NAMs for regulatory decision-making has highlighted the
importance of FAIR data so that diverse evidence streams can be interpreted, integrated and
reused throughout chemical safety assessment (Schmeisser et al., 2023; Hardy et al., 2024).
Unlike traditional animal studies, NAMs generate heterogeneous datasets spanning
high-content imaging, transcriptomics, metabolomics, *in vitro* assays, organ-on-chip models
and computational predictions (Colbourne et al., 2025; Serafini et al., 2024), platforms
whose disparate formats, metadata structures and normalisation approaches directly
complicate integration (Sheng et al., 2025). Their regulatory acceptance therefore depends
not only on scientific validity but also on standardised metadata, transparent provenance,
interoperable data formats and reproducible analytical workflows: the scientific-confidence
framework for NAMs rests on fitness for purpose, human biological relevance, technical
characterisation, data integrity and transparency, and independent review (van der Zalm et
al., 2022; see also Parish et al., 2020), and inter-laboratory reproducibility is itself
treated as a component of confidence distinct from validity (Jacobs et al., 2024). Where
reporting standards have been articulated, they have functioned as the precondition for
regulatory use of an omics NAM rather than as documentation overhead (Viant et al., 2019),
and open processing pipelines that record every analytical decision have been built for the
same reason (Filer et al., 2017).

Deepika et al. (2025) attribute the integration problem to two causes — heterogeneity of
format, structure and terminology across structured, semi-structured and unstructured
sources, and the absence of standardisation in generation and reporting — in contrast to
animal studies, whose established guidelines support risk-assessment use, and argue
ontology-based approaches as the route to machine-readable data and models, and thus to
predictive models robust enough to support IATA. The same constraint governs machine
learning on NAM evidence, which inherits its training data's inconsistency. The loss is
distributed across layers rather than concentrated at one point: raw instrument output,
processed data and interpreted information are separated by transformations that each
discard context, so that any change to a processing step alters the final output and the
pipeline must itself be documented in the reporting template (Blum et al., 2025; Krebs et
al., 2020). Interoperability has been identified as the principal obstacle to combining NAM
and legacy toxicology data for regulatory-grade analysis (Watford et al., 2019), and the
mechanistic scaffolding is subject to the same requirement: AOPs must themselves be FAIR to
act as an interoperability layer between assays and regulatory endpoints (Wittwehr et al.,
2024; Mortensen et al., 2025), a conversion demonstrated concretely by rendering the
AOP-Wiki as queryable RDF (Martens et al., 2022).

## An accumulating landscape of overlapping frameworks

Each gap has drawn a further reporting framework, and the frameworks now outnumber the
endpoints they were written to serve. Table 1 lists the principal instruments a NAM dataset
may be expected to satisfy; it is illustrative rather than exhaustive.

**Table 1.** Principal reporting and data-structuring instruments relevant to NAM evidence,
by the object each describes.

| Instrument | Custodian | Standardises | Object | Status |
|---|---|---|---|---|
| OECD Harmonised Templates | OECD | Study summary content per endpoint | Study summary | De facto mandatory for dossiers |
| IUCLID 6 | ECHA / OECD | Dossier structure, submission format | Substance dossier | Mandatory (REACH, biocides, PPP, AICIS) |
| OHT 201 "Intermediate effects" | OECD | Non-apical, mechanistic observations | Observation | Available; adoption emerging |
| CDISC SEND / SENDIG | CDISC | Nonclinical study tabulation | *In vivo* study | Mandatory (US FDA) |
| GD 34 | OECD | Validation and regulatory acceptance | Method | Guidance (under revision) |
| GD 211 | OECD | Non-guideline *in vitro* method description | Method | Guidance |
| ToxTemp | Academic community | Operationalisation of GD 211 | Method | Voluntary |
| GIVIMP (GD 286; 2nd ed. GD 421) | OECD | *In vitro* method quality practices | Method, facility | Guidance |
| GCCP 2.0 | Academic community | Cell and tissue culture practice | Test system | Voluntary |
| OORF (GD 390) | OECD | Transcriptomics / metabolomics reporting | Dataset | Guidance |
| MERIT reporting standards | Academic community | Metabolomics for regulatory use | Dataset | Voluntary |
| PBK model guidance (GD 331) | OECD | PBK model characterisation and reporting | Model | Guidance |
| QMRF / QPRF | JRC / ECHA | (Q)SAR model and prediction reporting | Model, prediction | Expected with QSAR evidence |
| FAIR / FAIR Lite principles for models | Academic community | FAIRness of computational models | Model | Voluntary |
| EFSA FAIR statement | EFSA | FAIR interpretation for effect models | Model | Statement |
| GD 417 | OECD | Generation, reporting, use of research data | Research dataset | Guidance |
| ISA, Bioschemas, RO-Crate | Community | Structure and packaging | Research object | Community standard |

Three properties make this landscape harder than the sum of its parts. The instruments
describe *different objects* — method, facility, test system, study summary, dataset, model,
prediction, dossier — with no maintained crosswalks, so conformance with one implies nothing
about another and the same experiment is re-described in several vocabularies. They differ
in legal standing, from treaty-backed acceptance through mandatory filing format and
guidance to voluntary convention, giving the producer no signal as to which subset suffices
for a given regulatory question. And the vocabulary of harmonisation is itself unharmonised:
EFSA glosses the final FAIR principle as reproducibility rather than reusability (EFSA,
2025).

The burden falls on the party least equipped to carry it. A laboratory generating a NAM
dataset for eventual regulatory use may face a funder's data-management requirement, a
journal data policy, GD 211 and ToxTemp for the method, GIVIMP and GCCP for quality
practice, the OORF for transcriptomics and OHT 201 for dossier entry — each with its own
template, terminology and granularity, none reading another's output. A single ToxTemp has
been estimated at up to five days (ONTOX, 2025); the aggregate predicts its own outcome in
partial and inconsistent compliance. That the community has felt this is evident in the
emergence of deliberately reduced schemes: having derived eighteen FAIR principles for
*in silico* toxicology models (Cronin et al., 2023) and shown by audit that published models
satisfy them poorly (Belfield et al., 2025), the same group compressed them to four
operational criteria on the explicit argument that a minimum viable subset is what gets
adopted (Cronin et al., 2025). The pattern is not redundancy but an unresolved division of
labour: each framework specifies what must be recorded about one object, none specifies a
machine-actionable container in which records and data travel together, and the integration
work is repeated by every producer and every assessor. The productive signals are
consolidative — OpenFoodTox into IUCLID and the OHTs (Benfenati et al., 2026), mechanistic
data into OHT 201 (Carnesecchi et al., 2023), legacy studies into SEND (Lauer et al., 2022)
— each reusing an existing carrier. What is required is therefore not a further reporting
standard but a packaging convention able to *carry* multiple conformance claims, binding an
explicit declaration of the standards a dataset adheres to to the data and method
descriptions those standards qualify.

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
prospective standardisation avoids. The comparable infrastructure on the NAM side, built to
manage and integrate heterogeneous *in vitro*, omics and *in silico* data for predictive
toxicology, emerged from EU-ToxRisk (Hardy et al., 2024).

## Academic and other non-standard data as regulatory evidence

A large body of academic data exists outside GLP and OECD Test Guidelines — and therefore
outside MAD — that nonetheless carries mechanistic, hazard or exposure information relevant
to regulatory decisions, and for endpoints poorly served by guideline studies, such as
developmental neurotoxicity, may be the only evidence available. Funder FAIR mandates and
journal data policies are making it progressively more accessible, but accessibility is not
the binding constraint. Reporting completeness is: reliability and relevance cannot be
judged when the test system is under-characterised, test substance identity and purity are
unstated, concentrations are nominal and unverified, or the statistical treatment cannot be
reconstructed. OECD Guidance Document 417 addresses this directly, structuring
recommendations around the research-data lifecycle — generation, reporting, identification,
evaluation, integration — and assigning them to distinct actors: funders, researchers,
publishers, repository managers, assessors and risk managers (OECD, 2025b). Its significance
is that it places obligations at the point of generation rather than asking assessors to
repair under-documented studies downstream. For data already in hand, structured appraisal
instruments bridge availability and use: the Klimisch categories (Klimisch et al., 1997),
operationalised and extended by ToxRTool (Schneider et al., 2009), CRED (Moermond et al.,
2016) and SciRAP (Beronius et al., 2018), each separating reliability from relevance and
making the basis of judgement explicit. These are complementary to FAIR, not substitutes for
it: FAIRness determines whether a study can be located and interrogated at acceptable cost,
these instruments whether it can be relied upon. A perfectly FAIR dataset can still be
regulatorily unusable.

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
this is general rather than *in vitro*-specific is shown by the computational and regulatory
literature converging on the same object: eighteen FAIR principles have been derived for
*in silico* toxicology models explicitly to improve regulatory acceptance of their
predictions (Cronin et al., 2023), and EFSA has interpreted each FAIR principle for
mechanistic effect models used in the environmental risk assessment of pesticides, arguing
that FAIRer models would yield a more efficient review process (EFSA, 2025). A model, like
an assay, is a method, and the object requiring FAIRification is the method and not only the
numbers it emits.

Making NAM-derived data FAIR concerns the individual experiment — which substance, which
biological model, which exposure design, which endpoint, which pipeline, which regulatory
question — and its infrastructure is ontological and structural rather than narrative:
persistent identifiers and controlled vocabularies for chemicals, cell lines, assays and
endpoints, minimum information models per data type, and a packaging convention keeping raw
data, processed data, scripts, protocols and metadata together as one machine-actionable
research object (Soiland-Reyes et al., 2022). That this does not happen spontaneously is
documented: a systematic FAIRification effort across physicochemical, bio-nano, toxicity,
omics, ecotoxicity and exposure data identified thirteen distinct challenges to be solved
before safety data become FAIR in practice (Jeliazkova et al., 2021). A third object
completes the set: a well-described method with no linked data cannot be evaluated on
evidence, well-packaged data pointing at an under-described method cannot be evaluated at
all, and both remain irreproducible unless the analytical workflow that produced the
reported result is captured and re-executable. Existing infrastructures cover these unevenly
— IUCLID and the OHTs the reported result, SEND the study tabulation, GD 211 and ToxTemp the
method description, the OORF omics reporting, EFSA's statement and the FAIR model principles
the model — but none binds method description, raw and processed data, analysis code and
regulatory context into one traceable, machine-actionable unit.

---

## References

Belfield, S. J., Basiri, H., Chavan, S., Chrysochoou, G., Enoch, S. J., Firman, J. W.,
Gomatam, A., Hardy, B., Helmke, P. S., Madden, J. C., Maran, U., March-Vila, E., Nikolov,
N. G., Pastor, M., Piir, G., Sild, S., Smajić, A., Spînu, N., Wedebye, E. B., & Cronin,
M. T. D. (2025). Moving towards making (quantitative) structure-activity relationships
((Q)SARs) for toxicity-related endpoints findable, accessible, interoperable and reusable
(FAIR). *ALTEX*, 42(4), 657–666. https://doi.org/10.14573/altex.2411161

Benfenati, E., et al. (2026). Further development and update of EFSA's Chemical Hazards
database: OpenFoodTox 3.0. *EFSA Supporting Publications*, EN-10099.
https://doi.org/10.2903/sp.efsa.2026.EN-10099

Beronius, A., Molander, L., Zilliacus, J., Rudén, C., & Hanberg, A. (2018). Testing and
refining the Science in Risk Assessment and Policy (SciRAP) web-based platform for
evaluating the reliability and relevance of *in vivo* toxicity studies. *Journal of Applied
Toxicology*, 38(12), 1460–1470.

Blum, J., et al. (2025). The long way from raw data to NAM-based information: Overview on
data layers and processing steps. *ALTEX*, 42(1), 167–180.
https://doi.org/10.14573/altex.2412171

Briggs, K., Bosc, N., Camara, T., Diaz, C., Drew, P., Drewe, W. C., Kors, J.,
van Mulligen, E., Pastor, M., Pognan, F., Quintana, J. R., Sarntivijai, S., &
Steger-Hartmann, T. (2021). Guidelines for FAIR sharing of preclinical safety and
off-target pharmacology data. *ALTEX*, 38(2), 187–197.
https://doi.org/10.14573/altex.2011181

Carnesecchi, E., Langezaal, I., Browne, P., Batista-Leite, S., Campia, I., Coecke, S.,
Dagallier, B., Deceuninck, P., Dorne, J. L. C. M., & Tarazona, J. V. (2023). OECD harmonised
template 201: Structuring and reporting mechanistic information to foster the integration of
new approach methodologies for hazard and risk assessment of chemicals. *Regulatory
Toxicology and Pharmacology*, 142, 105426.

Casati, S., Aschberger, K., Barroso, J., et al. (2018). Standardisation of defined approaches
for skin sensitisation testing to support regulatory use and international adoption: position
of the International Cooperation on Alternative Test Methods. *Archives of Toxicology*,
92(2), 611–617. https://doi.org/10.1007/s00204-017-2097-4

CDISC. (n.d.). *Standard for Exchange of Nonclinical Data (SEND)*.
https://www.cdisc.org/standards/foundational/send

Colbourne, J. K., Escher, S. E., Lee, R., Vinken, M., van de Water, B., & Freedman, J. H.
(2025). Animal-free Safety Assessment of Chemicals: Project Cluster for Implementation of
Novel Strategies (ASPIS) definition of new approach methodologies. *Environmental Toxicology
and Chemistry*, 44(9), 2395–2397. https://doi.org/10.1093/etojnl/vgae093

Cronin, M. T. D., Belfield, S. J., Briggs, K. A., Enoch, S. J., Firman, J. W., Frericks, M.,
Garrard, C., Maccallum, P. H., Madden, J. C., Pastor, M., Sanz, F., Soininen, I., & Sousoni,
D. (2023). Making in silico predictive models for toxicology FAIR. *Regulatory Toxicology
and Pharmacology*, 140, 105385. https://doi.org/10.1016/j.yrtph.2023.105385

Cronin, M. T. D., et al. (2025). The Findable, Accessible, Interoperable, Reusable (FAIR)
Lite Principles to ensure utility of computational toxicology models. *ALTEX*, 42(4).
https://doi.org/10.14573/altex.2502021

Deepika, Bharti, K., Sharma, S., Kumar, S., Pathak, R. K., Biosca Brull, J., Sabuz, O.,
García Vilana, S., & Kumar, V. (2025). Advancing human health risk assessment: the role of
new approach methodologies. *Frontiers in Toxicology*, 7, 1632941.
https://doi.org/10.3389/ftox.2025.1632941

EFSA. (2025). Statement on the interpretation of FAIR principles for mechanistic effect
models in the regulatory environmental risk assessment of pesticides. *EFSA Journal*,
23(11), e9741. https://doi.org/10.2903/j.efsa.2025.9741

FDA. (2024). *Study Data Technical Conformance Guide*. U.S. Food and Drug Administration.

Filer, D. L., Kothiya, P., Setzer, R. W., Judson, R. S., & Martin, M. T. (2017). tcpl: the
ToxCast pipeline for high-throughput screening data. *Bioinformatics*, 33(4), 618–620.
https://doi.org/10.1093/bioinformatics/btw680

Hardy, B., Mohoric, T., Exner, T., Dokler, J., Brajnik, M., Bachler, D., et al. (2024).
Knowledge infrastructure for integrated data management and analysis supporting new approach
methods in predictive toxicology and risk assessment. *Toxicology in Vitro*, 100, 105903.
https://doi.org/10.1016/j.tiv.2024.105903

Jacobs, M. N., et al. (2024). Avoiding a reproducibility crisis in regulatory toxicology — on
the fundamental role of ring trials. *Archives of Toxicology*, 98(7), 2047–2063.
https://doi.org/10.1007/s00204-024-03736-z

Jeliazkova, N., et al. (2021). Towards FAIR nanosafety data. *Nature Nanotechnology*, 16(6),
644–654. https://doi.org/10.1038/s41565-021-00911-6

Kavlock, R. J., Bahadori, T., Barton-Maclaren, T. S., Gwinn, M. R., Rasenberg, M., & Thomas,
R. S. (2018). Accelerating the pace of chemical risk assessment. *Chemical Research in
Toxicology*, 31(5), 287–290. https://doi.org/10.1021/acs.chemrestox.7b00339

Klimisch, H.-J., Andreae, M., & Tillmann, U. (1997). A systematic approach for evaluating
the quality of experimental toxicological and ecotoxicological data. *Regulatory Toxicology
and Pharmacology*, 25(1), 1–5.

Krebs, A., et al. (2019). Template for the description of cell-based toxicological test
methods to allow evaluation and regulatory use of the data. *ALTEX*, 36(4), 682–699.
https://doi.org/10.14573/altex.1909271 (Erratum: *ALTEX*, 37(1), 164.)

Krebs, A., et al. (2020). The EU-ToxRisk method documentation, data processing and chemical
testing pipeline for the regulatory use of new approach methods. *Archives of Toxicology*,
94(7), 2435–2461. https://doi.org/10.1007/s00204-020-02802-6

Lauer, K. B., Sarntivijai, S., Blomberg, N., et al. (2022). eTRANSAFE: Building a
sustainable framework to share reproducible drug safety knowledge with the public domain.
*F1000Research*, 11, 287.

Martens, M., Evelo, C. T., & Willighagen, E. L. (2022). Providing adverse outcome pathways
from the AOP-Wiki in a semantic web format to increase usability and accessibility of the
content. *Applied In Vitro Toxicology*, 8(1), 2–13. https://doi.org/10.1089/aivt.2021.0010

Moermond, C. T. A., Kase, R., Korkaric, M., & Ågerstrand, M. (2016). CRED: Criteria for
reporting and evaluating ecotoxicity data. *Environmental Toxicology and Chemistry*, 35(5),
1297–1309.

Mortensen, H. M., et al. (2025). The FAIR AOP roadmap for 2025: Advancing findability,
accessibility, interoperability, and re-usability of adverse outcome pathways.
*Computational Toxicology*, 35, 100368. https://doi.org/10.1016/j.comtox.2025.100368

OECD. (1981). *Decision of the Council concerning the Mutual Acceptance of Data in the
Assessment of Chemicals*, C(81)30/FINAL (OECD/LEGAL/0194).

OECD. (2017). *Guidance Document for Describing Non-Guideline In Vitro Test Methods* (Series
on Testing and Assessment No. 211).

OECD. (2018). *Guidance Document on Good In Vitro Method Practices (GIVIMP)* (Series on
Testing and Assessment No. 286). https://doi.org/10.1787/9789264304796-en (2nd ed. 2025,
No. 421.)

OECD. (2021). *Guidance Document on the Characterisation, Validation and Reporting of
Physiologically Based Kinetic (PBK) Models for Regulatory Purposes* (Series on Testing and
Assessment No. 331).

OECD. (2023). *OECD Omics Reporting Framework (OORF)* (Series on Testing and Assessment
No. 390).

OECD. (2025a). *Guidance Document on Good In Vitro Method Practices (GIVIMP), Second Edition*
(Series on Testing and Assessment No. 421). https://doi.org/10.1787/5ba6777b-en

OECD. (2025b). *Guidance Document on the Generation, Reporting and Use of Research Data for
Regulatory Assessments* (Series on Testing and Assessment No. 417).
https://doi.org/10.1787/8d49ec1d-en

OECD. (n.d.). *International Uniform ChemicaL Information Database (IUCLID)*.
https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/international-uniform-chemical-information-database.html

ONTOX. (2025). [ToxTemp completion-effort estimate — complete from project deliverable.]

Pamies, D., Leist, M., Coecke, S., Bowe, G., Allen, D. G., Gstraunthaler, G., Bal-Price, A.,
Pistollato, F., de Vries, R. B. M., Hogberg, H. T., Hartung, T., & Stacey, G. (2022).
Guidance document on Good Cell and Tissue Culture Practice 2.0 (GCCP 2.0). *ALTEX*, 39(1),
30–70. https://doi.org/10.14573/altex.2111011

Parish, S. T., Aschner, M., Casey, W., Corvaro, M., Embry, M. R., Fitzpatrick, S., Kidd, D.,
Kleinstreuer, N. C., Lima, B. S., Settivari, R. S., & Wolf, D. C. (2020). An evaluation
framework for new approach methodologies (NAMs) for human health safety assessment.
*Regulatory Toxicology and Pharmacology*, 112, 104592.

Schmeisser, S., Miccoli, A., von Bergen, M., Berggren, E., Braeuning, A., Busch, W., et al.
(2023). New approach methodologies in human regulatory toxicology – Not if, but how and when!
*Environment International*, 178, 108082. https://doi.org/10.1016/j.envint.2023.108082

Schneider, K., Schwarz, M., Burkholder, I., Kopp-Schneider, A., Edler, L.,
Kinsner-Ovaskainen, A., Hartung, T., & Hoffmann, S. (2009). "ToxRTool", a new tool to
assess the reliability of toxicological data. *Toxicology Letters*, 189(2), 138–144.

Serafini, M. M., et al. (2024). Recent advances and current challenges of new approach
methodologies in developmental and adult neurotoxicity testing. *Archives of Toxicology*,
98(5), 1271–1295. https://doi.org/10.1007/s00204-024-03703-8

Sheng, Q. S., Liu, B., Wang, X., et al. (2025). Revolutionizing toxicological risk
assessment: integrative advances in new approach methodologies (NAMs) and precision
toxicology. *Archives of Toxicology*, 99, 4697–4707.
https://doi.org/10.1007/s00204-025-04169-y

Soiland-Reyes, S., et al. (2022). Packaging research artefacts with RO-Crate.
*Data Science*, 5(2), 97–138.

UNECE. (2025). *Globally Harmonized System of Classification and Labelling of Chemicals
(GHS), Rev. 11*. United Nations Economic Commission for Europe.

van der Zalm, A. J., Barroso, J., Browne, P., Casey, W., Gordon, J., Henry, T. R.,
Kleinstreuer, N. C., Lowit, A. B., Perron, M., & Clippinger, A. J. (2022). A framework for
establishing scientific confidence in new approach methodologies. *Archives of Toxicology*,
96(11), 2865–2879. https://doi.org/10.1007/s00204-022-03365-4

Viant, M. R., Ebbels, T. M. D., Beger, R. D., Ekman, D. R., Epps, D. J. T., Kamp, H., et al.
(2019). Use cases, best practice and reporting standards for metabolomics in regulatory
toxicology. *Nature Communications*, 10, 3041. https://doi.org/10.1038/s41467-019-10900-y

Watford, S., Edwards, S., Angrish, M., Judson, R. S., & Paul Friedman, K. (2019). Progress
in data interoperability to support computational toxicology and chemical safety evaluation.
*Toxicology and Applied Pharmacology*, 380, 114707.
https://doi.org/10.1016/j.taap.2019.114707

Wilkinson, M. D., et al. (2016). The FAIR Guiding Principles for scientific data management
and stewardship. *Scientific Data*, 3, 160018. https://doi.org/10.1038/sdata.2016.18

Wittwehr, C., Clerbaux, L.-A., Edwards, S., Angrish, M., Mortensen, H., Carusi, A.,
Gromelski, M., Lekka, E., Virvilis, V., Martens, M., Bonino da Silva Santos, L.-O., &
Nymark, P. (2024). Why adverse outcome pathways need to be FAIR. *ALTEX*, 41(1), 50–56.
https://doi.org/10.14573/altex.2307131
