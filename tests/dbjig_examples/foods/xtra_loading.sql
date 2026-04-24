# This is a testing file that adds one fruit to the database and changes
# the color of apples

-- Add an apple
INSERT INTO fruits (name,color) VALUES ('grape','purple');
UPDATE fruits SET color='green' WHERE name = 'apple';




