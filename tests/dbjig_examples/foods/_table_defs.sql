CREATE TABLE fruits(
    name VARCHAR(30) NOT NULL PRIMARY KEY,
    color VARCHAR(30) NOT NULL
);


CREATE TABLE meats(
    name VARCHAR(30) NOT NULL PRIMARY KEY,
    source VARCHAR NOT NULL,
    average_fat_percent DECIMAL(5,2) 
);


-- meats_matching_source 
-- 
-- List all meats matching a given source.
-- THIS IS A PARAMETERIZED QUERY
SELECT *
FROM meats
WHERE source = :source;


