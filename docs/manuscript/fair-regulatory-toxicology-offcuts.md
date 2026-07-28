# Offcuts: material developed for the Regulatory Toxicology section that belongs elsewhere

Cut from the section to keep it proportionate to its siblings and to avoid duplicating
co-authors' subsections. Each block below is mapped to where it fits in the paper.

---

## → Section IV (Challenges and gaps): proliferation of overlapping reporting frameworks

This is the strongest cut item and does not currently exist anywhere in the paper. Section IV
lists "many types of IDs, ontologies, not all aligned" — the same problem exists one level up,
at the level of reporting frameworks.

A NAM dataset intended for regulatory use may be expected to satisfy: OECD Harmonised
Templates and OHT 201 (study summary, observation); IUCLID (dossier); CDISC SEND (study
tabulation); GD 34 (validation); GD 211 and ToxTemp (method description); GIVIMP, GD 286,
2nd ed. GD 421 (method and facility quality); GCCP 2.0 (test system); the OECD Omics
Reporting Framework, GD 390 (omics dataset); MERIT reporting standards (metabolomics); OECD
PBK model guidance GD 331, QMRF/QPRF and the FAIR / FAIR Lite model principles (model and
prediction); GD 417 (research dataset); and ISA, Bioschemas or RO-Crate (research object).

Three properties make this harder than the sum of its parts:

1. **Different objects, no crosswalks.** The instruments describe a method, a facility, a test
   system, a study summary, a dataset, a model, a prediction, a dossier or a research object.
   Conformance with one implies nothing about another, so the same experiment is re-described
   in several vocabularies.
2. **Heterogeneous legal standing** — from treaty-backed acceptance (MAD) through mandatory
   filing format (SEND, IUCLID) to guidance and voluntary convention. A producer gets no
   signal as to which subset suffices for a given regulatory question.
3. **The vocabulary of harmonisation is itself unharmonised.** EFSA glosses the final FAIR
   principle as reproducibility rather than reusability; more consequentially, most of the
   guidance that implements FAIR in practice — GD 211's completeness requirements, GIVIMP's
   record retention, the OORF's reporting elements — never uses the term, so conformance with
   FAIR cannot be read off conformance with the guidance or vice versa.

The burden falls on the party least equipped to carry it: a single laboratory facing a
funder's DMP requirement, a journal data policy, GD 211/ToxTemp, GIVIMP/GCCP, the OORF and
OHT 201, none of which reads another's output. A single ToxTemp has been estimated at up to
five days (ONTOX); the aggregate predicts partial and inconsistent compliance. That the
community feels this is evident in the emergence of deliberately reduced schemes — eighteen
FAIR principles for *in silico* models (Cronin et al., 2023), an audit showing published
models satisfy them poorly (Belfield et al., 2025), then compression to four operational
criteria on the argument that a minimum viable subset is what gets adopted (Cronin et al.,
2025).

**Argument this supports:** the requirement is not a further reporting standard but a
packaging convention able to *carry* multiple conformance claims — an explicit, machine-
readable declaration of which standards a dataset adheres to, bound to the data and method
descriptions those standards qualify. This connects directly to the paper's FAIR-by-design
thesis in Section 2.

---

## → Section IV (Challenges and gaps): FAIR does not ensure quality, but makes it assessable

Section IV already states this ("FAIR does not ensure quality but FAIR makes quality
assessable, which regulatory use and 3R objectives depend on… R1.2 provenance and R1.3
community standards"). The regulatory-toxicology literature supplies the concrete
operationalisation, which the current text lacks:

- Reliability and relevance are appraised by dedicated instruments — Klimisch categories
  (1997), ToxRTool (Schneider et al., 2009), CRED (Moermond et al., 2016), SciRAP (Beronius
  et al., 2018) — each separating reliability from relevance and making the basis of the
  judgement explicit.
- These are **complementary to FAIR, not substitutes**: FAIRness determines whether a study
  can be located and interrogated at acceptable cost; these instruments whether it can be
  relied upon. A perfectly FAIR dataset can still be regulatorily unusable.
- This is a clean answer to the Iseult/Jente discussion noted in Section IV: rich metadata
  (R1.2, R1.3) is precisely what makes these appraisal instruments applicable at scale, which
  is the mechanism by which FAIR serves quality without guaranteeing it.

A compressed version of this is retained in the Regulatory Toxicology section under academic
data, with a forward pointer; the fuller argument belongs here.

---

## → Section IV or V: the international dimension of NAM acceptance

Only MAD was retained in the section itself. The following is verified and available if the
paper wants to make the global-harmonisation point anywhere:

- **MAD detail.** Council Decision C(81)30/FINAL, 12 May 1981 (OECD/LEGAL/0194); legally
  binding on adherents; 45 countries = 38 OECD members + 7 non-member adherents (Argentina,
  Brazil, India, Malaysia, Singapore, South Africa, Thailand); open to non-members since 1997
  via a provisional→full ladder gated on evaluation of the national GLP compliance monitoring
  programme; OECD states savings of over EUR 309 million annually; China reported in talks
  toward provisional adherence.
- **ICATM** — International Cooperation on Alternative Test Methods, memorandum 2009 among
  the validation bodies of Canada, the EU, Japan and the US; Korea (KoCVAM) joined 2011;
  Brazil and China among observers. Its position paper on defined approaches for skin
  sensitisation led to an OECD test guideline (Casati et al., 2018, *Arch Toxicol*
  92(2):611–617).
- **APCRA** — Accelerating the Pace of Chemical Risk Assessment, founded 2016,
  government-to-government, North American / European / Asia-Pacific agencies (Kavlock et al.,
  2018, *Chem Res Toxicol* 31(5):287–290).
- **ICH** — founded 1990; since its 2015 reconstitution, regulatory membership spans Latin
  America, the Middle East, and East and Southeast Asia; the S-series harmonises nonclinical
  safety testing.
- **IMRWG3Rs** — International Medicines Regulators Working Group on 3Rs, established 2024
  (EMA, FDA, PMDA, Health Canada, TGA, Swissmedic), explicitly aiming at internationally
  harmonised 3Rs recommendations including NAM acceptance criteria. Directly relevant to the
  3R angle flagged in Section IV.
- **UN GHS Rev. 11** (2025) introduces guidance for classifying skin sensitisation using
  non-animal methods — NAM outputs entering a worldwide hazard-communication instrument.

**Caution, verified:** do **not** claim that Japan's PMDA or China's CDE require CDISC SEND.
Sources conflict; the weight of evidence is that nonclinical electronic submission is still
under discussion in both. Both have adopted the CDISC *clinical* standards (SDTM, ADaM).

---

## → Computational and predictive toxicology (already covered there — do not duplicate)

Cronin et al. (2023) eighteen FAIR principles; Cronin et al. (2025) FAIR Lite; Belfield et al.
(2025) *ALTEX* 42(4):657–666, the empirical audit of six published ML-QSARs against those
principles. The Belfield audit may be the one item not yet cited in that section and is worth
flagging to its author — it is the evidence that the principles are not being met in practice.

## → AOP section (already covered there — do not duplicate)

Wittwehr et al. (2024) — note the correct citation is *ALTEX* **41(1):50–56**, doi
10.14573/altex.2307131; one aggregator gives 40(4), which is wrong. Mortensen et al. (2025)
FAIR AOP roadmap is *Computational Toxicology* **35:100368**, doi
10.1016/j.comtox.2025.100368 — not *Current Opinion in Toxicology*.

## → Nanotoxicology section (already covered there — do not duplicate)

Jeliazkova et al. (2021), *Nature Nanotechnology* 16(6):644–654 — thirteen enumerated
FAIRification challenges across physicochemical, bio-nano, toxicity, omics, ecotoxicity and
exposure data. Strong evidence that FAIR-by-default does not happen spontaneously; useful to
the nanotoxicology author if not already cited.

## → Experimental / in vitro section (already covered there)

Hardy et al. (2024), *Toxicology in Vitro* 100:105903 — the EU-ToxRisk knowledge
infrastructure. Its abstract states the infrastructure "supports FAIR data for New Approach
Methods". Already cited there and in Section 2.

---

## Reference corrections found during this work, applicable paper-wide

| Item | Correction |
|---|---|
| OHT 201 paper | **Carnesecchi et al. 2023**, *Regul Toxicol Pharmacol* 142:105426 — not Wittwehr |
| Wittwehr AOP FAIR | *ALTEX* **41(1)**:50–56 (not 40(4)) |
| Mortensen FAIR AOP roadmap | *Computational Toxicology* **35**:100368 |
| ELIXIR Toxicology community paper | cite **v2, 2023**, doi 10.12688/f1000research.74502.2 — the peer-reviewed version, not the 2021 v1 |
| GIVIMP 2nd edition | OECD Series **No. 421** (2025) — not a reissue under No. 286 |
| GD 211 | approved 2014, published 2017 — choose per citation style |
| GD 34 | under revision, completion expected 2027 |
| ToxTemp (Krebs et al. 2019) | has an erratum: *ALTEX* 37(1):164 |
| Deepika et al. 2025 | first author is a **mononym** ("Deepika"), nine authors total — not "Deepika, D., Bharti, K. & Sharma, S." |
| Blum et al. 2025 | doi 10.14573/altex.2412171 |

**All of the above came from search-result metadata; no source was opened in full (session
network policy blocked Crossref, doi.org, PMC, ScienceDirect, OECD and Frontiers). Verify in
Zotero before submission.**

---

## Open question for the section author

The paper's ecosystem intro already cites Deepika et al. for the claim that NAMs need data to
be FAIR by design. The Regulatory Toxicology draft previously summarised the same paper at
paragraph length, which duplicated that. The summary has been removed from the current draft;
if it is wanted, it should be shortened and positioned as elaboration of the earlier citation
rather than as a fresh introduction of the source.

Separately: two independent searches found no evidence that FAIR is a substantive theme in
Deepika et al. — it is a broad NAM review (QSAR, read-across, PBK, AOP, IATA, in vitro
models) with "data harmonization" among its keywords. Whoever has read it should confirm the
weight it is being asked to carry.
