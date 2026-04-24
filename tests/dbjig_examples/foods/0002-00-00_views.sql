

CREATE VIEW meat_count AS 
SELECT count(*) as count from meats;

CREATE TABLE fruits_copy AS
SELECT * FROM fruits;

DELETE FROM fruits_copy;

INSERT INTO fruits_copy (name,color) VALUES ('mango','red-green');

