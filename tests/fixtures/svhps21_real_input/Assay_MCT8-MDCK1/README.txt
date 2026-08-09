# Assay README

## Assay Title
Inhibition of MCT8-mediated cellular uptake of triiodothyronine in an overexpressing cell model

## Assay Description

Context
The monocarboxylate 8 (MCT8, expressed by the SLC16A2 gene) is a transmembrane protein that is important for cellular uptake of triiodothyronine (T3) in the brain. It facilitates the transport of T3 across the cell membrane endothelial cells in syncytiotrophoblasts and cytotrophoblasts cells in the placenta, in endothelial cell in the blood-brain-barrier (BBB), and in choroid plexus epithelial cells in the blood-cerebrospinal fluid-barrier (BCSFB). As such, MCT8 plays a crucial role in the transport of maternal TH across three barriers (placenta, BBB, BCSFB) to the developing fetal brain. Disruption of MCT8 mediated T3 transport across the barriers might cause TH deficiency in the brain, which can lead to severe adverse neurodevelopmental effects. The importance of MCT8 for TH supply to the brain is elucidated by patients carrying mutations in the MCT8 encoding gene SLC16A2, leading to a severe neurodevelopment disorder called the Allan-Herndon-Dudley syndrome (AHDS). Certain chemicals can interfere with the transport of T3 into cells by inhibition of MCT8. The disruption of the MCT8 facilitated thyroid hormone transmembrane transport is regarded as a possible mechanism for thyroid hormone system disruption by chemicals.

Principle
Madin-Darby canine kidney 1 (MDCK1) cells have been stably transfected with a plasmid encoding the gene for MCT8 and, as a result, overexpress MCT8 on their membrane. After exposure to 10 µM of T3 for 30 minutes, cells are washed to remove extracellular T3. Intracellular T3 is measured as iodine, which is first digested from the T3 by UV irradiation and quantified as the rate of the Sandell-Kolthoff reaction, an iodide-catalyzed colorimetric reaction where Ce4+ (yellow) is reduced to Ce3+ (colorless).To determine the MCT8 inhibiting capacity of a test item, cells are co-exposed to 10 µM of T3 for 30 minutes in combination with a concentration of test item or a solvent control. If the test item is capable of inhibiting MCT8 mediated uptake of T3, less iodide will be digested from the intracellular T3 in cells exposed to the test item compared to cells exposed to the solvent control, which is quantified as a decreased Sandell-Kolthoff reaction rate. Sandell-Kolthoff reaction rates can be calculated into T3 concentrations using a calibration curve of T3 in the Sandell-Kolthoff reaction.
If a test item causes a concentration-dependent decrease in cellular uptake of T3, a concentration-response curve is fitted to the intracellular T3 concentration, and its MCT8 inhibiting capacity is expressed as a benchmark concentration (BMC20/IC20). To exclude that a decrease in cellular uptake of T3 is due to cytotoxicity of the test item to the cells, cell viability assays are run in parallel with the same cells exposed to the test item under similar exposure conditions as in the T3 uptake experiments. The assay has been developed by Jayarama-Naidu et al. (2015), and has been modified with respect to UV-digestion and data analysis by Wagenaars et al. (2024).

## Contributors
- Fabian Wagenaars 
- Vrije Universiteit, Amsterdam

- Timo Hamers
- Vrije Universiteit, Amsterdam

- Martin Scholze 
- Brunel University, London

## File Organization
There are four types of files added in the current folder
1. Assay Meta data
This summarizes the meta data of the assay in a simple template format

2. Raw data and individual processed data
This contains the raw data and processed data for each individual experiment. 
Each experiment contains two files:
- An excel file, which contains the raw data and calculations to convert raw data in processed data (e.g. percentage of control data) 
- An graphpad file, which is used to perform regression analysis on the raw and processed data and to visualize the data from each individual experiment

3.Study wide processed data 
This contains the collected processed data of each biological experiment for the entire study. 
The "data for statistical analysis" contains the the summarized processed data and calculations on interstudy statistics
The two other graphpad files contains the finalized data for both the cytotoxicity assay and the MCT8-MDCK1 assay itself 

4. Standard Operating Procedure (SOP)
This contains a detailed description of the methodology, context and definition of the assay

## License
[Default CC-BY 4.0 for data, CC0 for metadata unless specified otherwise]


## Comments
As of now only data regarding silycrhistin has been added to the folder, this will be extended with all raw data. 