FROM postgres:16-bookworm

# 1. Install Build Dependencies
RUN apt-get update && apt-get install -y \
    postgresql-server-dev-16 \
    gcc \
    make \
    git \
    build-essential \
    libcurl4-openssl-dev \
    flex \
    bison \
    pkg-config \
    libxml2-dev

# 2. Install pgvector (v0.7.0 is stable)
RUN cd /tmp && \
    git clone --branch v0.7.0 https://github.com/pgvector/pgvector.git && \
    cd pgvector && \
    make && \
    make install

# 3. Install pgsql-http
RUN cd /tmp && \
    git clone https://github.com/pramsey/pgsql-http.git && \
    cd pgsql-http && \
    make && \
    make install

# 4. Install Apache AGE (Graph) for PG16
RUN cd /tmp && \
    git clone https://github.com/apache/age.git && \
    cd age && \
    # AGE versioning can be tricky, ensuring we are on a PG16 compatible branch/tag
    git checkout PG16 && \
    make && \
    make install

# 5. Cleanup to keep image small
RUN rm -rf /tmp/* && \
    apt-get purge -y --auto-remove \
    postgresql-server-dev-16 \
    gcc \
    make \
    git \
    build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# First-boot initialization. initdb executes *.sql in sorted filename order:
# 000_schema.sql (the frozen v0.1.0 baseline), then migrations 001..NNN, then
# zzz_initdb_stamp.sql records the resulting schema level. Fresh installs and
# upgraded installs therefore converge on the same schema (CI proves it).
COPY schema.sql /docker-entrypoint-initdb.d/000_schema.sql
COPY migrations/ /docker-entrypoint-initdb.d/
