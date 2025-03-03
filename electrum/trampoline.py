import os
import bitstring
import random

from .logging import get_logger, Logger
from .lnutil import LnFeatures
from .lnonion import calc_hops_data_for_payment, new_onion_packet
from .lnrouter import RouteEdge, TrampolineEdge, LNPaymentRoute, is_route_sane_to_use
from .lnutil import NoPathFound, LNPeerAddr
from . import constants


_logger = get_logger(__name__)

# trampoline nodes are supposed to advertise their fee and cltv in node_update message
TRAMPOLINE_FEES = [
    {
        'fee_base_msat': 0,
        'fee_proportional_millionths': 0,
        'cltv_expiry_delta': 576,
    },
    {
        'fee_base_msat': 1000,
        'fee_proportional_millionths': 100,
        'cltv_expiry_delta': 576,
    },
    {
        'fee_base_msat': 3000,
        'fee_proportional_millionths': 100,
        'cltv_expiry_delta': 576,
    },
    {
        'fee_base_msat': 5000,
        'fee_proportional_millionths': 500,
        'cltv_expiry_delta': 576,
    },
    {
        'fee_base_msat': 7000,
        'fee_proportional_millionths': 1000,
        'cltv_expiry_delta': 576,
    },
    {
        'fee_base_msat': 12000,
        'fee_proportional_millionths': 3000,
        'cltv_expiry_delta': 576,
    },
    {
        'fee_base_msat': 100000,
        'fee_proportional_millionths': 3000,
        'cltv_expiry_delta': 576,
    },
]

# hardcoded list
# TODO for some pubkeys, there are multiple network addresses we could try
TRAMPOLINE_NODES_MAINNET = {
    'ACINQ': LNPeerAddr(host='34.239.230.56', port=9735, pubkey=bytes.fromhex('03864ef025fde8fb587d989186ce6a4a186895ee44a926bfc370e2c366597a3f8f')),
    'Electrum trampoline': LNPeerAddr(host='144.76.99.209', port=9740, pubkey=bytes.fromhex('03ecef675be448b615e6176424070673ef8284e0fd19d8be062a6cb5b130a0a0d1')),
}
TRAMPOLINE_NODES_TESTNET = {
    'endurance': LNPeerAddr(host='34.250.234.192', port=9735, pubkey=bytes.fromhex('03933884aaf1d6b108397e5efe5c86bcf2d8ca8d2f700eda99db9214fc2712b134')),
}

def hardcoded_trampoline_nodes():
    if constants.net in (constants.BitcoinMainnet, ):
        return TRAMPOLINE_NODES_MAINNET
    if constants.net in (constants.BitcoinTestnet, ):
        return TRAMPOLINE_NODES_TESTNET
    return {}

def trampolines_by_id():
    return dict([(x.pubkey, x) for x in hardcoded_trampoline_nodes().values()])

is_hardcoded_trampoline = lambda node_id: node_id in trampolines_by_id().keys()

def encode_routing_info(r_tags):
    result = bitstring.BitArray()
    for route in r_tags:
        result.append(bitstring.pack('uint:8', len(route)))
        for step in route:
            pubkey, channel, feebase, feerate, cltv = step
            result.append(bitstring.BitArray(pubkey) + bitstring.BitArray(channel) + bitstring.pack('intbe:32', feebase) + bitstring.pack('intbe:32', feerate) + bitstring.pack('intbe:16', cltv))
    return result.tobytes()


def create_trampoline_route(
        *,
        amount_msat:int,
        min_cltv_expiry:int,
        invoice_pubkey:bytes,
        invoice_features:int,
        my_pubkey: bytes,
        trampoline_node_id,
        r_tags,
        trampoline_fee_level: int,
        use_two_trampolines: bool) -> LNPaymentRoute:

    invoice_features = LnFeatures(invoice_features)
    if invoice_features.supports(LnFeatures.OPTION_TRAMPOLINE_ROUTING_OPT)\
        or invoice_features.supports(LnFeatures.OPTION_TRAMPOLINE_ROUTING_OPT_ECLAIR):
        is_legacy = False
    else:
        is_legacy = True

    # fee level. the same fee is used for all trampolines
    if trampoline_fee_level < len(TRAMPOLINE_FEES):
        params = TRAMPOLINE_FEES[trampoline_fee_level]
    else:
        raise NoPathFound()
    # add optional second trampoline
    trampoline2 = None
    if is_legacy and use_two_trampolines:
        trampoline2_list = list(trampolines_by_id().keys())
        random.shuffle(trampoline2_list)
        for node_id in trampoline2_list:
            if node_id != trampoline_node_id:
                trampoline2 = node_id
                break
    # node_features is only used to determine is_tlv
    trampoline_features = LnFeatures.VAR_ONION_OPT
    # hop to trampoline
    route = []
    # trampoline hop
    route.append(
        TrampolineEdge(
            start_node=my_pubkey,
            end_node=trampoline_node_id,
            fee_base_msat=params['fee_base_msat'],
            fee_proportional_millionths=params['fee_proportional_millionths'],
            cltv_expiry_delta=params['cltv_expiry_delta'],
            node_features=trampoline_features))
    if trampoline2:
        route.append(
            TrampolineEdge(
                start_node=trampoline_node_id,
                end_node=trampoline2,
                fee_base_msat=params['fee_base_msat'],
                fee_proportional_millionths=params['fee_proportional_millionths'],
                cltv_expiry_delta=params['cltv_expiry_delta'],
                node_features=trampoline_features))
    # add routing info
    if is_legacy:
        invoice_routing_info = encode_routing_info(r_tags)
        route[-1].invoice_routing_info = invoice_routing_info
        route[-1].invoice_features = invoice_features
        route[-1].outgoing_node_id = invoice_pubkey
    else:
        last_trampoline = route[-1].end_node
        r_tags = [x for x in r_tags if len(x) == 1]
        random.shuffle(r_tags)
        for r_tag in r_tags:
            pubkey, scid, feebase, feerate, cltv = r_tag[0]
            if pubkey == trampoline_node_id:
                break
        else:
            pubkey, scid, feebase, feerate, cltv = r_tag[0]
            if route[-1].node_id != pubkey:
                route.append(
                    TrampolineEdge(
                        start_node=route[-1].node_id,
                        end_node=pubkey,
                        fee_base_msat=feebase,
                        fee_proportional_millionths=feerate,
                        cltv_expiry_delta=cltv,
                        node_features=trampoline_features))

    # Final edge (not part of the route if payment is legacy, but eclair requires an encrypted blob)
    route.append(
        TrampolineEdge(
            start_node=route[-1].end_node,
            end_node=invoice_pubkey,
            fee_base_msat=0,
            fee_proportional_millionths=0,
            cltv_expiry_delta=0,
            node_features=trampoline_features))
    # check that we can pay amount and fees
    for edge in route[::-1]:
        amount_msat += edge.fee_for_edge(amount_msat)
    if not is_route_sane_to_use(route, amount_msat, min_cltv_expiry):
        raise NoPathFound()
    _logger.info(f'created route with trampoline: fee_level={trampoline_fee_level}, is legacy: {is_legacy}')
    _logger.info(f'first trampoline: {trampoline_node_id.hex()}')
    _logger.info(f'second trampoline: {trampoline2.hex() if trampoline2 else None}')
    _logger.info(f'params: {params}')
    return route


def create_trampoline_onion(*, route, amount_msat, final_cltv, total_msat, payment_hash, payment_secret):
    # all edges are trampoline
    hops_data, amount_msat, cltv = calc_hops_data_for_payment(
        route,
        amount_msat,
        final_cltv,
        total_msat=total_msat,
        payment_secret=payment_secret)
    # detect trampoline hops.
    payment_path_pubkeys = [x.node_id for x in route]
    num_hops = len(payment_path_pubkeys)
    for i in range(num_hops):
        route_edge = route[i]
        assert route_edge.is_trampoline()
        payload = hops_data[i].payload
        if i < num_hops - 1:
            payload.pop('short_channel_id')
            next_edge = route[i+1]
            assert next_edge.is_trampoline()
            hops_data[i].payload["outgoing_node_id"] = {"outgoing_node_id":next_edge.node_id}
        # only for final
        if i == num_hops - 1:
            payload["payment_data"] = {
                "payment_secret":payment_secret,
                "total_msat": total_msat
            }
        # legacy
        if i == num_hops - 2 and route_edge.invoice_features:
            payload["invoice_features"] = {"invoice_features":route_edge.invoice_features}
            payload["invoice_routing_info"] = {"invoice_routing_info":route_edge.invoice_routing_info}
            payload["payment_data"] = {
                "payment_secret":payment_secret,
                "total_msat": total_msat
            }
        _logger.info(f'payload {i} {payload}')
    trampoline_session_key = os.urandom(32)
    trampoline_onion = new_onion_packet(payment_path_pubkeys, trampoline_session_key, hops_data, associated_data=payment_hash, trampoline=True)
    return trampoline_onion, amount_msat, cltv


def create_trampoline_route_and_onion(
        *,
        amount_msat,
        total_msat,
        min_cltv_expiry,
        invoice_pubkey,
        invoice_features,
        my_pubkey: bytes,
        node_id,
        r_tags,
        payment_hash,
        payment_secret,
        local_height:int,
        trampoline_fee_level: int,
        use_two_trampolines: bool):
    # create route for the trampoline_onion
    trampoline_route = create_trampoline_route(
        amount_msat=amount_msat,
        min_cltv_expiry=min_cltv_expiry,
        my_pubkey=my_pubkey,
        invoice_pubkey=invoice_pubkey,
        invoice_features=invoice_features,
        trampoline_node_id=node_id,
        r_tags=r_tags,
        trampoline_fee_level=trampoline_fee_level,
        use_two_trampolines=use_two_trampolines)
    # compute onion and fees
    final_cltv = local_height + min_cltv_expiry
    trampoline_onion, amount_with_fees, bucket_cltv = create_trampoline_onion(
        route=trampoline_route,
        amount_msat=amount_msat,
        final_cltv=final_cltv,
        total_msat=total_msat,
        payment_hash=payment_hash,
        payment_secret=payment_secret)
    bucket_cltv_delta = bucket_cltv - local_height
    bucket_cltv_delta += trampoline_route[0].cltv_expiry_delta
    # trampoline fee for this very trampoline
    trampoline_fee = trampoline_route[0].fee_for_edge(amount_with_fees)
    amount_with_fees += trampoline_fee
    return trampoline_onion, amount_with_fees, bucket_cltv_delta
