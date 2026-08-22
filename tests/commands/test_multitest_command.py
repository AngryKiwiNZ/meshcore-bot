from types import SimpleNamespace

from modules.commands.multitest_command import MultitestCommand
from modules.models import MeshMessage


def make_command():
    command = object.__new__(MultitestCommand)
    command.bot = SimpleNamespace(
        prefix_hex_chars=2,
        message_handler=SimpleNamespace(),
    )
    command.logger = SimpleNamespace(debug=lambda *args, **kwargs: None)
    return command


def test_extracts_direct_route_from_rf_data():
    command = make_command()
    rf_data = {'routing_info': {'path_length': 0, 'path_nodes': []}}
    assert command.extract_path_from_rf_data(rf_data) == 'Direct'


def test_extracts_direct_route_from_message():
    command = make_command()
    message = MeshMessage(content='mt', path='Direct via DIRECT')
    assert command.extract_path_from_message(message) == 'Direct'


def test_extracts_multibyte_routed_path():
    command = make_command()
    rf_data = {
        'routing_info': {
            'path_length': 2,
            'bytes_per_hop': 2,
            'path_nodes': ['e484', '0262'],
        }
    }
    assert command.extract_path_from_rf_data(rf_data) == 'e484,0262'


def test_falls_back_to_shared_packet_path_decoder():
    command = make_command()
    command.bot.message_handler._get_path_from_rf_data = lambda rf: ('aa,bb', ['aa', 'bb'], 2)
    assert command.extract_path_from_rf_data({'routing_info': {'path_length': 2}}) == 'aa,bb'
