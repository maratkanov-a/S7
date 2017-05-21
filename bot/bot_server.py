# coding=utf-8
import json
import os
import redis
import logging

import requests
import tornado.ioloop
import tornado.web
import tornado.wsgi
import tornado.gen

from imaged import scan_photos, detect_photos
from config import URL_TO_WEB, AMOUNT_OF_PHOTOS, PAGE_ACCESS_TOKEN, VERIFY_TOKEN, NUM_TREDS
from concurrent import futures

executor = futures.ThreadPoolExecutor(NUM_TREDS)


class BotHandler(tornado.web.RequestHandler):
    @tornado.gen.coroutine
    def post(self):
        """
        Получаем письмо от бота
        """
        self.finish()
        # self.clean_cashe()
        if not self._was_messages:
            self.send_hello_message()
        else:
            if self._has_image:
                yield executor.submit(self.photos_processing)
            elif self.message.get('payload') == u'USER_WANTS_TOGETHER_1727539463938068':
                self.send_message(self.sender, u'Мы оповестим человека, о желании лететь с ним :)')
            else:
                self.send_message(self.sender, u'Мне нужны фотографии, чтобы я мог найти '
                                               u'Вам попутчика')

    def get(self):
        """
        Верификация бота
        """
        if self._verification:
            self.write(self.verify_token)
        else:
            self.write('Hello from FB bot')

    @property
    def verify_token(self):
        if self.request.query_arguments.get("hub.mode")[0] == "subscribe" and self.request.query_arguments.get(
                "hub.challenge"):
            if not self.request.query_arguments.get("hub.verify_token")[0] == VERIFY_TOKEN:
                self.write_error(403)
            return self.request.query_arguments["hub.challenge"][0]

    @property
    def _verification(self):
        return self.request.query_arguments.get("hub.mode", False)

    @property
    def _was_messages(self):
        return r.get(self.sender + '_was')

    @property
    def _has_image(self):
        return self.message.get('attachments') and self.message['attachments'][0]['type'] == u'image'

    @property
    def previous_state(self):
        return r.get(self.sender)

    @property
    def photos_now(self):
        return len(json.loads(r.get(self.sender)))

    @property
    def photos_left(self):
        return AMOUNT_OF_PHOTOS - self.photos_now

    @property
    def _messaging(self):
        return self._data[0].get('messaging')[0]

    @property
    def _data(self):
        return json.loads(self.request.body).get('entry')

    @property
    def sender(self):
        return self._messaging['sender']['id']

    @property
    def message(self):
        return self._messaging['message'] if self._messaging.get('message') else {"attachments": {}, "payload": self._messaging.get('postback', {}).get('payload')}

    @property
    def list_attachements(self):
        return [attachment['payload']['url'] for attachment in self.message['attachments']]

    @property
    def big_sender(self):
        raw = requests.get("https://graph.facebook.com/v2.6/{}?fields=first_name,last_name,profile_pic&access_token={}"
                           .format(self.sender, PAGE_ACCESS_TOKEN))
        return json.loads(raw.content)

    def set_first_state(self):
        r.set(self.sender, json.dumps(self.list_attachements))
        self.list_of_photos = self.list_attachements

    def update_state(self, url):
        self.list_of_photos = json.loads(self.previous_state)
        self.list_of_photos.append(url)
        r.set(self.sender, json.dumps(self.list_of_photos))

    def send_hello_message(self):
        self.send_message(self.sender, u'Пройдите по ссылке {} или же пришлите мне {} фотографий,'
                                  u'для подбора собеседника'.format(URL_TO_WEB, AMOUNT_OF_PHOTOS))
        r.set(self.sender + '_was', '1')

    def clean_cashe(self):
        r.delete(self.sender)
        r.delete(self.sender+'_was')

    def parse_photos(self):
        [self.update_state(attachment) for attachment in self.list_attachements]

    def send_result_message(self, recipient_id, reply):
        for key, value in reply['photos'].iteritems():
            self.match_message(recipient_id, reply.get('avatars', {}).get(key), u'Совпадения с {}'.format(key))
            elements = []
            i = 1
            self.send_message(recipient_id, u'Вы можете поговорить с {} про: {}'.format(key, ', '.join(reply['themes'][key])))
            for item in value:
                elements.append({
                     "title": u'Ваше фото',
                     "subtitle": u"{}/5 наиболее похожее фото".format(i),
                     "image_url": item['user'],
                 })
                elements.append(
                    {
                        "title": u'Фотограия {}'.format(key),
                        "subtitle": u"{} {}/5 наиболее совпавшее фото".format(key,i),
                        "image_url": item['city'],
                    }
                )
                i += 1
            self.send_carousel(recipient_id, elements)

    @tornado.gen.coroutine
    def photos_processing(self):
        if not self.previous_state:
            self.set_first_state()
        else:
            self.parse_photos()
        if self.photos_left <= 0:
            self.send_message(self.sender, u'Спасибо за фотографии, мы начали поиск !')
            self.photos_analyzing(self.list_of_photos)
        else:
            self.send_message(self.sender, u'Осталось еще {} фотографии'.format(self.photos_left))
        raise tornado.gen.Return()

    def photos_analyzing(self, list_of_photos):
        for_set, for_scan = detect_photos(list_of_photos)
        reply = scan_photos(for_set=for_set, for_scan=for_scan, sender=self.sender, big_sender=self.big_sender)
        self.send_message(self.sender,
                          u'Вам наиболее подходят следующие пользователи: {}, {}, {}'.format(reply['cities'][0],
                                                                                             reply['cities'][1],
                                                                                             reply['cities'][2]))
        self.send_result_message(self.sender, reply)
        r.delete(self.sender)

    @staticmethod
    def send_carousel(recipient_id, elements):
        params = {
            "access_token": PAGE_ACCESS_TOKEN
        }
        headers = {
            "Content-Type": "application/json"
        }
        data = json.dumps({
            "recipient": {
                "id": recipient_id
            },
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "generic",
                        "elements": elements
                    }
                }
            }
        })
        requests.post("https://graph.facebook.com/v2.6/me/messages", params=params, headers=headers, data=data)

    @staticmethod
    def send_attachment(recipient_id, url):
        params = {
            "access_token": PAGE_ACCESS_TOKEN
        }
        headers = {
            "Content-Type": "application/json"
        }
        data = json.dumps({
            "recipient": {
                "id": recipient_id
            },
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {
                        "url": url
                    }
                }
            }
        })
        requests.post("https://graph.facebook.com/v2.6/me/messages", params=params, headers=headers, data=data)

    @staticmethod
    def send_message(recipient_id, message_text):
        params = {
            "access_token": PAGE_ACCESS_TOKEN
        }
        headers = {
            "Content-Type": "application/json"
        }
        data = json.dumps({
            "recipient": {
                "id": recipient_id
            },
            "message": {
                "text": message_text
            }
        })
        requests.post("https://graph.facebook.com/v2.6/me/messages", params=params, headers=headers, data=data)

    @staticmethod
    def match_message(recipient_id, img_url, message_text):
        params = {
            "access_token": PAGE_ACCESS_TOKEN
        }
        data = {
            "recipient": {
                "id": recipient_id
            },
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "generic",
                        "elements": [
                            {
                                "title": message_text,
                                "image_url": img_url,
                                "buttons": [
                                    {
                                        "type": "postback",
                                        "title": "Хочу лететь вместе",
                                        "payload": "USER_WANTS_TOGETHER_{}".format(recipient_id)
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        requests.post("https://graph.facebook.com/v2.6/me/messages", params=params, json=data)

    @classmethod
    def start_message(cls):
        params = {
            "access_token": PAGE_ACCESS_TOKEN
        }
        data = {
            "setting_type": "call_to_actions",
            "thread_state": "new_thread",
            "call_to_actions": [
                {
                    "payload": "USER_DEFINED_PAYLOAD"
                }
            ]
        }
        requests.post("https://graph.facebook.com/v2.6/me/thread_settings", params=params, json=data)

        params = {
            "access_token": PAGE_ACCESS_TOKEN
        }
        data = {
            "greeting": [
                {
                    "locale": "default",
                    "text": "Привет, мы подберем тебе лучшего в мире поптчика :)"
                },
                {
                    "locale": "en_US",
                    "text": "Timeless apparel for the masses."
                },
                {
                    "locale": "ru_RU",
                    "text": "Привет, мы подберем тебе лучшего в мире поптчика :)"
                }
            ]
        }
        requests.post("https://graph.facebook.com/v2.6/me/messenger_profile", params=params, json=data)


def make_app():
    settings = {
        "static_path": os.path.join(os.path.dirname(__file__), "templates"),
        "static_url_prefix": "/imagepals/static"
    }
    BotHandler.start_message()
    return tornado.web.Application([
        (r"/bot", BotHandler)],
        **settings)


if __name__ == "__main__":
    app = make_app()
    app.listen(8080)
    r = redis.StrictRedis(host='localhost', port=6379, db=0)
    logging.basicConfig(filename='bot_log.log', level=logging.INFO,
                        format='%(asctime)s [%(name)s.%(levelname)s] {%(process)d/%(thread)d} %(message)s')
    tornado.ioloop.IOLoop.current().start()
