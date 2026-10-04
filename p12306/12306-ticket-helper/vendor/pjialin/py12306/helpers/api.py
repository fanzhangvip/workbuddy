# coding=utf-8

HOST_URL_OF_12306 = 'kyfw.12306.cn'
BASE_URL_OF_12306 = 'https://' + HOST_URL_OF_12306

LEFT_TICKETS = {
    "url": BASE_URL_OF_12306 + "/otn/{type}?leftTicketDTO.train_date={left_date}&leftTicketDTO.from_station={left_station}&leftTicketDTO.to_station={arrive_station}&purpose_codes=ADULT",
}

API_BASE_LOGIN = {
    "url": BASE_URL_OF_12306 + '/passport/web/login',
}

API_USER_LOGIN_CHECK = BASE_URL_OF_12306 + '/otn/login/conf'

API_AUTH_QRCODE_BASE64_DOWNLOAD = {
    'url': BASE_URL_OF_12306 + '/passport/web/create-qr64'
}

API_AUTH_QRCODE_CHECK = {
    'url': BASE_URL_OF_12306 + '/passport/web/checkqr'
}

API_USER_LOGIN = {
    'url': BASE_URL_OF_12306 + '/otn/login/userLogin'
}

API_AUTH_CODE_DOWNLOAD = {
    'url': BASE_URL_OF_12306 + '/passport/captcha/captcha-image?login_site=E&module=login&rand=sjrand&_={random}'
}
API_AUTH_CODE_BASE64_DOWNLOAD = BASE_URL_OF_12306 + '/passport/captcha/captcha-image64?login_site=E&module=login&rand=sjrand&_={random}'
API_AUTH_CODE_CHECK = {
    'url': BASE_URL_OF_12306 + '/passport/captcha/captcha-check?answer={answer}&rand=sjrand&login_site=E&_={random}'
}
API_AUTH_UAMTK = {
    'url': BASE_URL_OF_12306 + '/passport/web/auth/uamtk'
}
API_AUTH_UAMAUTHCLIENT = {
    'url': BASE_URL_OF_12306 + '/otn/uamauthclient'
}

API_USER_INFO = {
    'url': BASE_URL_OF_12306 + '/otn/modifyUser/initQueryUserInfoApi'
}
API_USER_PASSENGERS = BASE_URL_OF_12306 + '/otn/confirmPassenger/getPassengerDTOs'
API_SUBMIT_ORDER_REQUEST = BASE_URL_OF_12306 + '/otn/leftTicket/submitOrderRequest'
API_CHECK_ORDER_INFO = BASE_URL_OF_12306 + '/otn/confirmPassenger/checkOrderInfo'
API_INITDC_URL = BASE_URL_OF_12306 + '/otn/confirmPassenger/initDc'  # 生成订单时需要先请求这个页面
API_GET_QUEUE_COUNT = BASE_URL_OF_12306 + '/otn/confirmPassenger/getQueueCount'
API_CONFIRM_SINGLE_FOR_QUEUE = BASE_URL_OF_12306 + '/otn/confirmPassenger/confirmSingleForQueue'
API_QUERY_ORDER_WAIT_TIME = BASE_URL_OF_12306 + '/otn/confirmPassenger/queryOrderWaitTime?{}'  # 排队查询
API_QUERY_INIT_PAGE = BASE_URL_OF_12306 + '/otn/leftTicket/init'
# [TICKET_HELPER 修改] 改回官方地址：上游把这里换成了自家的托管服务，
# 而本项目的需求方明确禁止请求该域。
# 改回官方地址后， query.py / user/job.py 里的
#   if 'pjialin' not in API_GET_BROWSER_DEVICE_ID:  return self.request_device_id2()
# 会判为真，自动走上游自己留的直连官方路径。
API_GET_BROWSER_DEVICE_ID = BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'
# [TICKET_HELPER 修改] 删除了 API_FREE_CODE_QCR_API（原值指向 https://12306-ocr.pjialin.com/check/ ）。
# 原因同上：不请求该托管域。打码改走上游的 'user' 模式（Config().API_USER_CODE_QCR_API）。

API_NOTIFICATION_BY_VOICE_CODE = 'http://ali-voice.showapi.com/sendVoice?'
API_NOTIFICATION_BY_VOICE_CODE_DINGXIN = 'http://yuyin2.market.alicloudapi.com/dx/voice_notice'

API_CHECK_CDN_AVAILABLE = 'https://{}/otn/dynamicJs/omseuuq'
