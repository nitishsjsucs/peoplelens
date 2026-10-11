{#
  Time columns that MetricFlow aggregates on. BigQuery: TIMESTAMP, because
  MetricFlow renders TIMESTAMP_TRUNC(col, month) for BigQuery. DuckDB: DATE.
#}
{% macro pl_time(expr) -%}
  {%- if target.type == 'bigquery' -%}
    cast({{ expr }} as timestamp)
  {%- else -%}
    cast({{ expr }} as date)
  {%- endif -%}
{%- endmacro %}

{# A series of month starts, inclusive, as DATE. #}
{% macro pl_month_series(start_date, end_date, column_name='month_start') -%}
  {%- if target.type == 'bigquery' -%}
    select {{ column_name }} from unnest(generate_date_array(date '{{ start_date }}', date '{{ end_date }}', interval 1 month)) as {{ column_name }}
  {%- else -%}
    select cast(gs as date) as {{ column_name }} from generate_series(date '{{ start_date }}', date '{{ end_date }}', interval 1 month) as t(gs)
  {%- endif -%}
{%- endmacro %}

{# A series of days, inclusive, as DATE. #}
{% macro pl_day_series(start_date, end_date, column_name='date_day') -%}
  {%- if target.type == 'bigquery' -%}
    select {{ column_name }} from unnest(generate_date_array(date '{{ start_date }}', date '{{ end_date }}', interval 1 day)) as {{ column_name }}
  {%- else -%}
    select cast(gs as date) as {{ column_name }} from generate_series(date '{{ start_date }}', date '{{ end_date }}', interval 1 day) as t(gs)
  {%- endif -%}
{%- endmacro %}

{# Last day of the month that starts at a DATE expression. #}
{% macro pl_month_end(month_start_expr) -%}
  {%- if target.type == 'bigquery' -%}
    last_day({{ month_start_expr }}, month)
  {%- else -%}
    last_day({{ month_start_expr }})
  {%- endif -%}
{%- endmacro %}

{# 64-bit float on both engines (dbt.type_float() is 32-bit FLOAT on DuckDB). #}
{% macro pl_type_double() -%}
  {%- if target.type == 'bigquery' -%}float64{%- else -%}double{%- endif -%}
{%- endmacro %}
