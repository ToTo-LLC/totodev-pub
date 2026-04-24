

create table vehicles(
    id integer primary key,
    name text NOT NULL,
    year integer,
    make text,
    model text,
    vin text,
    color text,
    mileage integer,
    price real,
    description text
);
