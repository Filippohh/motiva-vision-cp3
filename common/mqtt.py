"""Criação de cliente MQTT compatível com paho-mqtt 1.x e 2.x."""
import paho.mqtt.client as mqtt

from config import settings


def make_client(client_id, will_topic=None, will_payload=None):
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id=client_id, clean_session=True)
    if settings.BROKER_USER:
        client.username_pw_set(settings.BROKER_USER, settings.BROKER_PASS)
    if will_topic:
        client.will_set(will_topic, will_payload, qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    return client
