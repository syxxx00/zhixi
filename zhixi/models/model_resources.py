"""Bounded startup admission, not a prediction of a model's running peak.

Budget host backing for the weight file, configured prompt cache, and working
headroom. Full-offload requests also budget the weight file on one GPU; partial
offload only checks GPU headroom because file size cannot reveal layer sizes.
No processes are stopped and no device/model is silently changed here.
"""

import math

from zhixi.ops.ops_device import device_status

MIB = 1024**2


class ModelResourceError(RuntimeError):
    def __init__(self, message, observation):
        super().__init__(message)
        self.resource_observation = {**observation, 'status': 'refused'}


def _bytes(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def check_model_resources(metadata, *, cache_ram_mib=0, gpu_layers=None, resident=False, snapshot=None):
    """Check fresh machine headroom immediately before startup or reuse.

    The conservative file-size budget may reject a model that could run with
    paging; this is intentional admission policy, not proof of impossibility.
    Missing GPU telemetry is disclosed, never converted to zero usage. Unknown
    physical RAM cannot pass. Other programs can allocate after this snapshot.
    """
    snapshot = device_status(fresh=True) if snapshot is None else snapshot
    memory = snapshot.get('memory') or {}
    available = _bytes(memory.get('available_bytes'))
    if available is None:
        total, used = _bytes(memory.get('total_bytes')), _bytes(memory.get('used_bytes'))
        if total is not None and used is not None and used <= total:
            available = total - used
    weight = _bytes(metadata.get('weight_bytes'))
    if not resident and (weight is None or weight <= 0):
        raise ModelResourceError('未能取得模型权重大小，未启动本地模型。', {'status': 'unknown_weight_size'})
    transformer = metadata.get('runtime_kind') == 'transformers'
    # HF first loads host weights before moving to CUDA. The margin is a
    # budget, not an architecture-independent calculation of actual allocation.
    weight_budget = math.ceil((weight or 0) * (1.25 if transformer else 1))
    ram_budget = 1024 * MIB + (0 if resident else weight_budget + cache_ram_mib * MIB)
    wants_gpu = gpu_layers != 0 if gpu_layers is not None else transformer
    full_gpu = transformer or (gpu_layers is not None and gpu_layers >= 99)
    gpu_budget = (512 * MIB + (weight_budget if full_gpu and not resident else 0)) if wants_gpu else 0
    receipt = {
        'status': 'checked', 'scope': 'startup_budget_not_peak_guarantee',
        'resident': resident, 'sampled_at': snapshot.get('sampled_at'),
        'weight_bytes': weight, 'required_ram_bytes': ram_budget,
        'available_ram_bytes': available, 'required_gpu_bytes': gpu_budget,
        'gpu_budget_scope': ('not_requested' if not wants_gpu else
                             'weight_file_and_headroom' if full_gpu else 'headroom_only'),
        'gpu_observation': 'not_requested' if not wants_gpu else 'not_checked',
    }
    if available is None:
        raise ModelResourceError('无法核对当前可用内存，未启动新的模型调用。', receipt)
    if available < ram_budget:
        raise ModelResourceError(
            f'本地模型启动预算不足：可用内存 {available / 1024**3:.2f} GiB，'
            f'本次预算需 {ram_budget / 1024**3:.2f} GiB；未启动新的模型调用。', receipt)
    commit = _bytes(memory.get('commit_available_bytes'))
    receipt['commit_available_bytes'] = commit
    if commit is not None and commit < ram_budget:
        raise ModelResourceError('系统可用提交额度不足，未启动新的模型调用。', receipt)
    cards = snapshot.get('gpus') or []
    if wants_gpu:
        receipt['gpu_observation'] = 'unavailable'
    if wants_gpu and cards:
        # Do not sum independent GPUs or choose a different device behind the
        # caller's back. For multiple cards the loader's placement is unknown.
        if len(cards) == 1:
            card = cards[0]
            total, used = _bytes(card.get('total_mib')), _bytes(card.get('used_mib'))
            if total is not None and used is not None and used <= total:
                free = (total - used) * MIB
                receipt.update(gpu_observation='single_card', available_gpu_bytes=free,
                               gpu_name=card.get('name'))
                if free < gpu_budget:
                    raise ModelResourceError(
                        f'本地模型启动预算不足：可用显存 {free / 1024**3:.2f} GiB，'
                        f'本次预算需 {gpu_budget / 1024**3:.2f} GiB；未启动新的模型调用。', receipt)
        else:
            receipt['gpu_observation'] = 'multiple_cards_placement_unverified'
    if commit is None or (wants_gpu and receipt['gpu_observation'] != 'single_card'):
        receipt['status'] = 'partially_checked'
    return receipt
