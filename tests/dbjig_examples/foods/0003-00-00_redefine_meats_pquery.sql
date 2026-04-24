
# The below query was already defined in an earlier file.  
# Redefining it should quietly replace the old definition with this one.

-- meats_matching_source 
-- 
-- List all meats matching a given source, ordered by name.
-- THIS IS A PARAMETERIZED QUERY
SELECT *
FROM meats
WHERE source = :source
ORDER BY name;

