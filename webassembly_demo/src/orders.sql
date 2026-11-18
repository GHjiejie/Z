WITH input AS (
    SELECT trim(city) AS city, trim(amount) AS amount
    FROM read_csv('orders.csv', header = true, delim = ',', all_varchar = true)
), validated AS (
    SELECT
        CASE WHEN coalesce(city, '') = '' THEN error('城市不能为空') ELSE city END AS city,
        CASE
            WHEN regexp_full_match(amount, '[0-9]+([.][0-9]{1,2})?')
                AND try_cast(amount AS DECIMAL(18, 2)) IS NOT NULL
            THEN cast(amount AS DECIMAL(18, 2))
            ELSE error('金额须为非负十进制数，最多两位小数和十六位整数')
        END AS amount
    FROM input
)
SELECT city, count(*) AS orders, cast(sum(amount) AS VARCHAR) AS revenue
FROM validated
GROUP BY city
ORDER BY sum(amount) DESC, city;
