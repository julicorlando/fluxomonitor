FROM eclipse-mosquitto:2
COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY start.sh /usr/local/bin/start-mqtt
RUN chmod 755 /usr/local/bin/start-mqtt
ENTRYPOINT ["/usr/local/bin/start-mqtt"]
