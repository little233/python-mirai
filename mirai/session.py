import typing as T
from urllib import parse
from .network import fetch
from .protocol import MiraiProtocol
from .group import Group
from .friend import Friend
from .message.types import FriendMessage, GroupMessage, MessageTypes, MessageItemType
from .event import InternalEvent, ExternalEvent, ExternalEventTypes, ExternalEvents
import asyncio
from threading import Thread, Lock
import concurrent.futures

import random

class Session(MiraiProtocol):
  cache_options: T.Dict[str, bool] = {}

  cached_groups: T.List[Group] = []
  cached_friends: T.List[Friend] = []

  enabled: bool = False

  event_stacks: asyncio.Queue
  event: T.Dict[T.Union[GroupMessage, FriendMessage], T.List[T.Dict[
    T.Callable[
      [ # message
        T.Union[FriendMessage, GroupMessage]
      ], bool],
    T.Callable[
      [ # message, session, parent_protocol
        T.Union[FriendMessage, GroupMessage], "Session", "MiraiProtocol"
      ], T.Awaitable[T.Any]
    ]
  ]]] = {}

  async_runtime: Thread = None
  another_loop: asyncio.AbstractEventLoop
  exit_signal: bool = False

  def __init__(self, 
    url: T.Optional[str] = None, # 

    host: T.Optional[str] = None,
    port: T.Optional[int] = None,
    authKey: T.Optional[str] = None,
    qq: T.Optional[int] = None,

    cache_groups: T.Optional[bool] = True,
    cache_friends: T.Optional[bool] = True
  ):
    if url:
      urlinfo = parse.urlparse(url)
      if urlinfo:
        query_info = parse.parse_qs(urlinfo.query)
        if all((
          urlinfo.scheme == "mirai",
          urlinfo.path == "/",

          "authKey" in query_info and query_info["authKey"],
          "qq" in query_info and query_info["qq"]
        )):
          # 确认过了, 无问题
          authKey = query_info["authKey"][0]

          self.baseurl = f"http://{urlinfo.netloc}"
          self.auth_key = authKey
          self.qq = query_info["qq"][0]
        else:
          raise ValueError("invaild url: wrong format")
      else:
        raise ValueError("invaild url")
    else:
      if all([host, port, authKey, qq]): 
        self.baseurl = f"http://{host}:{port}"
        self.auth_key = authKey
        self.qq = qq
      else:
        raise ValueError("invaild arguments")

    self.cache_options['groups'] = cache_groups
    self.cache_options['friends'] = cache_friends

    self.shared_lock = Lock()
    self.another_loop = asyncio.new_event_loop()
    self.event_stacks = asyncio.Queue(loop=self.another_loop)

  async def enable_session(self) -> "Session":
    auth_response = await super().auth()
    if all([
      "code" in auth_response and auth_response['code'] == 0,
      "session" in auth_response and auth_response['session'] or\
        "msg" in auth_response and auth_response['msg'] # polyfill
    ]):
      if "msg" in auth_response and auth_response['msg']:
        self.session_key = auth_response['msg']
      else:
        self.session_key = auth_response['session']

      await super().verify()
    else:
      if "code" in auth_response and auth_response['code'] == 1:
        raise ValueError("invaild authKey")
      else:
        raise ValueError('invaild args: unknown response')
    
    if self.cache_options['groups']:
      self.cached_groups = await super().groupList()
    if self.cache_options['friends']:
      self.cached_friends = await super().friendList()

    self.enabled = True
    return self

  @classmethod
  async def start(cls,
    url: T.Optional[str] = None,

    host: T.Optional[str] = None,
    port: T.Optional[int] = None,
    authKey: T.Optional[str] = None,
    qq: T.Optional[int] = None,

    cache_groups: T.Optional[bool] = True,
    cache_friends: T.Optional[bool] = True,
  ):
    self = cls(url, host, port, authKey, qq, cache_groups, cache_friends)
    return await self.enable_session()

  def setting_event_runtime(self):
    async def connect():
      with self.shared_lock:
        await asyncio.wait([
          self.event_runner(lambda: self.exit_signal, self.event_stacks),
          self.message_polling(lambda: self.exit_signal, self.event_stacks)
        ])
    def inline_warpper(loop: asyncio.AbstractEventLoop):
      asyncio.set_event_loop(loop)
      loop.create_task(connect())
      loop.run_forever()
    self.async_runtime = Thread(target=inline_warpper, args=(self.another_loop,), daemon=True)

  def start_event_runtime(self):
    self.async_runtime.start()

  async def __aenter__(self) -> "Session":
    #self.polling_runtime.__enter__() # 短轮询运行时启动
    #self.event_runtime.__enter__()
    #self.shared_runtime.__enter__()
    self.setting_event_runtime()
    self.start_event_runtime()
    return await self.enable_session()

  async def __aexit__(self, exc_type, exc, tb):
    await self.close_session(ignoreError=True)

  async def message_polling(self, exit_signal_status, queue, count=10):
    while not exit_signal_status():
      await asyncio.sleep(0.5)

      result: T.List[T.Union[FriendMessage, GroupMessage, ExternalEvent]] = \
        await super().fetchMessage(count)
      last_length = len(result)
      latest_result = []
      while True:
        if last_length == count:
          latest_result = await super().fetchMessage(count)
          last_length = len(latest_result)
          result += latest_result
          continue
        break
      
      # 开始处理
      # 事件系统实际上就是"lambda", 指定事件名称(like. GroupMessage), 然后lambda判断.
      # @event.receiver("GroupMessage", lambda info: info.......)
      for message_index in range(len(result)):
        await queue.put(
          InternalEvent(
            name=result[message_index].type.value\
              if isinstance(result[message_index].type, MessageItemType) else \
                result[message_index].type,
            body=result[message_index]
          )
        )

  def receiver(self, event_name, 
      addon_condition: T.Optional[
        T.Callable[[T.Union[FriendMessage, GroupMessage]], bool]
      ] = None):
    def receiver_warpper(
      func: T.Callable[
        [ # message, session, parent_protocol
          T.Union[FriendMessage, GroupMessage], "Session", "MiraiProtocol"
        ], T.Awaitable[T.Any]
      ]
    ):
      if event_name not in self.event:
        self.event[event_name] = [{addon_condition: func}]
      else:
        self.event[event_name].append({addon_condition: func})
      return func
    return receiver_warpper

  async def event_runner(self, exit_signal_status, queue: asyncio.Queue):
    while not exit_signal_status():
      event_context: InternalEvent
      try:
        event_context: InternalEvent = await asyncio.wait_for(queue.get(), 2)
      except asyncio.exceptions.TimeoutError:
        if exit_signal_status():
          break
        else:
          continue
      if event_context.name in self.event:
        for event in self.event[event_context.name]:
          if event: # 判断是否有注册.
            for pre_condition, run_body in event.items():
              if not pre_condition:
                await run_body(event_context.body, self, super())
                continue
              if pre_condition(event_context.body):
                await run_body(event_context.body, self, super())

  async def close_session(self, ignoreError=False):
    if self.enabled:
      self.exit_signal = True
      while self.shared_lock.locked():
        pass
      else:
        self.another_loop.call_soon_threadsafe(self.another_loop.stop)
        self.async_runtime.join()
      await super().release()
      self.enabled = False
    else:
      if not ignoreError:
        raise ConnectionAbortedError("session closed.")

  async def stop_event_runtime(self):
    if not self.async_runtime:
      raise ConnectionError("runtime stoped.")
    self.exit_signal = True
    while self.shared_lock.locked():
      pass
    else:
      self.another_loop.call_soon_threadsafe(self.another_loop.stop)
      self.async_runtime.join()