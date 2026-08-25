FROM rabbitmq:4.2-management

COPY rabbitmq.conf /etc/rabbitmq/rabbitmq.conf
COPY docker-entrypoint-with-secrets.sh /usr/local/bin/docker-entrypoint-with-secrets

RUN chmod 0555 /usr/local/bin/docker-entrypoint-with-secrets \
    && chmod 0444 /etc/rabbitmq/rabbitmq.conf

ENTRYPOINT ["docker-entrypoint-with-secrets"]
CMD ["rabbitmq-server"]
