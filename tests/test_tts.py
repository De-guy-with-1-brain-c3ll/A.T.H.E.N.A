import os
import base64
import threading
import time
import dashscope
import pyaudio
from dashscope.audio.qwen_tts_realtime import AudioFormat, QwenTtsRealtime, QwenTtsRealtimeCallback
from athena.config import load_local_environment


qwen_tts_realtime: QwenTtsRealtime = None
text_to_synthesize = [
  'Good evening, Benjamin. This is a longer real-time speech synthesis test.',
  'You should begin hearing this message while Qwen is still converting the sentences that follow.',
  'Every completed chunk of raw PCM audio will be sent directly to the speakers as soon as it arrives.',
  'The program will continue receiving and playing new chunks without waiting for the complete audio file.',
  'This is how ATHENA can start speaking a response while the rest of the answer is still being generated.',
  'If you can hear this before the session-finished message appears, then real-time playback is working correctly.'
]

DO_VIDEO_TEST = False

def init_dashscope_api_key():
  """
    设置 DashScope API Key。详细信息请参见：
    https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/master/PREREQUISITES.md
  """

  load_local_environment()
  key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
  if not key:
    raise RuntimeError("Add DASHSCOPE_API_KEY to the project .env file first.")
  dashscope.api_key = key



class MyCallback(QwenTtsRealtimeCallback):
  def __init__(self):
    self.complete_event = threading.Event()
    self.file = open('result_24k.pcm', 'wb')
    self.player = pyaudio.PyAudio()
    self.stream = self.player.open(
      format=pyaudio.paInt16,
      channels=1,
      rate=24000,
      output=True
    )

  def on_open(self) -> None:
    print('连接已建立，初始化播放器')

  def on_close(self, close_status_code, close_msg) -> None:
    self.file.close()
    self.stream.stop_stream()
    self.stream.close()
    self.player.terminate()
    print('连接已关闭，状态码：{}，消息：{}，销毁播放器'.format(close_status_code, close_msg))

  def on_event(self, response: dict) -> None:
    try:
      event_type = response['type']
      if event_type == 'session.created':
        print('会话已开始：{}'.format(response['session']['id']))
      elif event_type == 'response.audio.delta':
        pcm_chunk = base64.b64decode(response['delta'])
        self.file.write(pcm_chunk)
        self.stream.write(pcm_chunk)
      elif event_type == 'response.done':
        print('语音响应完成')
      elif event_type == 'session.finished':
        print('会话已结束')
        self.complete_event.set()
    except Exception as error:
      print('[错误] {}'.format(error))

  def wait_for_finished(self):
    self.complete_event.wait()


if __name__  == '__main__':
  init_dashscope_api_key()

  print('正在初始化...')

  callback = MyCallback()

  qwen_tts_realtime = QwenTtsRealtime(
    model='qwen3-tts-flash-realtime',
    callback=callback,
    url='wss://dashscope.aliyuncs.com/api-ws/v1/realtime'
  )

  qwen_tts_realtime.connect()
  qwen_tts_realtime.update_session(
    voice='Neil',
    response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
    mode='server_commit'
  )
  for text_chunk in text_to_synthesize:
    print(f'发送文本：{text_chunk}')
    qwen_tts_realtime.append_text(text_chunk)
    time.sleep(0.1)
  qwen_tts_realtime.finish()
  callback.wait_for_finished()
  print('[Metric] session: {}, first audio delay: {} ms'.format(
          qwen_tts_realtime.get_session_id(),
          qwen_tts_realtime.get_first_audio_delay(),
          ))
