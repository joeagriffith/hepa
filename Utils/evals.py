import torch
import torch.nn as nn
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from Utils.dataset import PreloadedDataset
from tqdm import tqdm
import torch.nn.functional as F

from Examples.ModelNet10.dataset import ModelNet10, ModelNet10Simple
from Examples.MNIST.dataset import MNIST
from Utils.functional import feature_correlation, feature_std, feature_entropy

def linear_probing(
    model: nn.Module,
    writer: SummaryWriter,
    n_per_class: int,
    cfg: dict,
    finetune: bool = False,
):
    device = torch.device(cfg['device'])

    # Create classifier and specify training parameters
    classifier = nn.Sequential(
        nn.BatchNorm1d(model.num_features, affine=False) if cfg['bn_output'] else nn.Identity(),
        nn.Linear(model.num_features, 10, bias=False),
    ).to(device)
    batch_size = max(n_per_class, 10)
    num_epochs = 100 if cfg['dataset'] == 'mnist' else 200
    lr = 0.1

    if cfg['dataset'] == 'mnist':
        train = MNIST(cfg['root'], split='train', n=n_per_class, device=cfg['device'], use_tqdm=cfg['local'])
        val = MNIST(cfg['root'], split='val', device=cfg['device'], use_tqdm=cfg['local'])

    elif cfg['dataset'] == 'modelnet10':
        train = ModelNet10Simple(cfg['root'], split='train', n=n_per_class, device=cfg['device'], use_tqdm=cfg['local'], rank=cfg['ddp_rank'], world_size=cfg['ddp_world_size'], seed=cfg['seed'])
        val = ModelNet10Simple(cfg['root'], split='val', n=10, device=cfg['device'], use_tqdm=cfg['local'], rank=cfg['ddp_rank'], world_size=cfg['ddp_world_size'], seed=cfg['seed'])

    train_loader = DataLoader(train, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val, batch_size=1000, shuffle=False)

    param_dict = {pn: p for pn, p in classifier.named_parameters()}
    param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
    decay_params = [p for n, p in param_dict.items() if 'bn' not in n and 'bias' not in n]
    nondecay_params = [p for n, p in param_dict.items() if 'bn' in n or 'bias' in n]

    if finetune:
        encoder = model.copy()
        encoder.train()
        
        param_dict = {pn: p for pn, p in encoder.named_parameters()}
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
        decay_params = decay_params + [p for n, p in param_dict.items() if 'bn' not in n and 'bias' not in n]
        nondecay_params = nondecay_params + [p for n, p in param_dict.items() if 'bn' in n or 'bias' in n]
    else:
        encoder = model
        encoder.eval()

    optim_groups = [
        {'params': decay_params, 'weight_decay': 0.005}, 
        {'params': nondecay_params, 'weight_decay': 0.0},
    ]

    optimiser = torch.optim.AdamW(optim_groups, lr=lr)
    sched_step_size = 30 if cfg['dataset'] == 'mnist' else 60
    scheduler = torch.optim.lr_scheduler.StepLR(optimiser, step_size=sched_step_size, gamma=0.1) 

    last_train_loss = torch.tensor(-1, device=device)
    last_train_acc = torch.tensor(-1, device=device)
    last_val_loss = torch.tensor(-1, device=device)
    last_val_acc = torch.tensor(-1, device=device)
    best_val_acc = torch.tensor(-1, device=device)

    postfix = {}
    for epoch in range(num_epochs):
        classifier.train()
        if finetune:
            encoder.train()

        if cfg['local']:
            loop = tqdm(enumerate(train_loader), total=len(train_loader), leave=False)
            loop.set_description(f'Epoch [{epoch}/{num_epochs}]')
            if epoch > 0:
                loop.set_postfix(postfix)
        else:
            loop = enumerate(train_loader)
        epoch_train_loss = torch.zeros(len(train_loader), device=device)
        epoch_train_acc = torch.zeros(len(train_loader), device=device)
        for i, (x, y) in loop:
            x = x.to(device)
            y = y.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16):
                z = encoder(x)
                y_pred = classifier(z)
                loss = F.cross_entropy(y_pred, y)
            
            classifier.zero_grad(set_to_none=True)
            if finetune:
                optimiser.zero_grad(set_to_none=True)
            loss.backward()
            optimiser.step()

            epoch_train_loss[i] = loss.detach()
            epoch_train_acc[i] = (y_pred.argmax(dim=1) == y).float().mean().detach()

        last_train_loss = epoch_train_loss.mean()
        last_train_acc = epoch_train_acc.mean()

        scheduler.step()
        
        with torch.no_grad():
            classifier.eval()
            if finetune:
                encoder.eval()

            epoch_val_loss = torch.zeros(len(val_loader), device=device)
            epoch_val_acc = torch.zeros(len(val_loader), device=device)
            for i, (x, y) in enumerate(val_loader):
                x = x.to(device)
                y = y.to(device)
                with torch.autocast(device_type=device.type, dtype=torch.bfloat16):
                    z = encoder(x)
                    y_pred = classifier(z)
                    loss = F.cross_entropy(y_pred, y)
                epoch_val_loss[i] += loss.detach()
                epoch_val_acc[i] += (y_pred.argmax(dim=1) == y).float().mean().detach()

            last_val_loss = epoch_val_loss.mean().detach() 
            last_val_acc = epoch_val_acc.mean().detach()
            if last_val_acc > best_val_acc:
                best_val_acc = last_val_acc
        
        if writer is not None:
            if finetune:
                writer.add_scalar('train/finetuning_loss', last_train_loss.item(), epoch)
                writer.add_scalar('train/finetuning_accuracy', last_train_acc.item(), epoch)
                writer.add_scalar('val/finetuning_loss', last_val_loss.item(), epoch)
                writer.add_scalar('val/finetuning_accuracy', last_val_acc.item(), epoch)
            else:
                writer.add_scalar('train/loss', last_train_loss.item(), epoch)
                writer.add_scalar('train/accuracy', last_train_acc.item(), epoch)
                writer.add_scalar('val/loss', last_val_loss.item(), epoch)
                writer.add_scalar('val/accuracy', last_val_acc.item(), epoch)
        
        postfix = {
            'train_loss': last_train_loss.item(),
            'train_accuracy': last_train_acc.item(),
            'val_loss': last_val_loss.item(),
            'val_accuracy': last_val_acc.item(),
        }

    if cfg['dataset'] == 'mnist':
        t_dataset = datasets.MNIST(root='../Datasets/', train=False, transform=transforms.ToTensor(), download=True)
    elif cfg['dataset'] == 'modelnet10':
        t_dataset = ModelNet10Simple(cfg['root'], split='test', device=cfg['device'], use_tqdm=cfg['local'], rank=cfg['ddp_rank'], world_size=cfg['ddp_world_size'], seed=cfg['seed'])
    test = PreloadedDataset.from_dataset(t_dataset, transforms.ToTensor(), device, use_tqdm=cfg['local'])
    test_loader = DataLoader(test, batch_size=100, shuffle=False)

    test_accs = torch.zeros(len(test_loader), device=device)
    with torch.no_grad():
        for i, (x, y) in enumerate(test_loader):
            x = x.to(device)
            y = y.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16):
                z = encoder(x)
                y_pred = classifier(z)
            test_accs[i] = (y_pred.argmax(dim=1) == y).float().mean()

    test_acc = test_accs.mean().item()
    if writer is not None:
        if finetune:
            writer.add_scalar('test/finetuning_accuracy', test_acc)
        else:
            writer.add_scalar('test/accuracy', test_acc)
    print(f'N: {n_per_class} - Test accuracy: {test_acc}')

def one_step_linear_probing(
        encoder,
        train_loader,
        val_loader,
):
    encoder.eval()
    device = next(encoder.parameters()).device

    classifier = nn.Sequential(
        # nn.BatchNorm1d(encoder.num_features, affine=False),
        nn.Linear(encoder.num_features, 10, bias=False),
    ).to(device)
    optimiser = torch.optim.AdamW(classifier.parameters(), lr=1e-1, weight_decay=0.0)

    for i, (x, y) in enumerate(train_loader):
        x = x.to(device)
        y = y.to(device)
        with torch.autocast(device_type=device.type, dtype=torch.bfloat16):
            with torch.no_grad():
                z = encoder(x)

            y_pred = classifier(z)
            loss = F.cross_entropy(y_pred, y)

        optimiser.zero_grad(set_to_none=True)
        loss.backward()
        optimiser.step()

    val_accs = torch.zeros(len(val_loader), device=device)
    val_losses = torch.zeros(len(val_loader), device=device)
    classifier.eval()
    with torch.no_grad():
        for i, (x, y) in enumerate(val_loader):
            x = x.to(device)
            y = y.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16):
                with torch.no_grad():
                    z = encoder(x)        
                y_pred = classifier(z)
            val_accs[i] = (y_pred.argmax(dim=1) == y).float().mean()
            val_losses[i] = F.cross_entropy(y_pred, y)

    val_acc = val_accs.mean().item()
    val_loss = val_losses.mean().item()

    return val_acc, val_loss


def get_rep_metrics(
    model: nn.Module,
    dataset: PreloadedDataset,
    cfg: dict,
):
    device = next(model.parameters()).device
    loader = DataLoader(dataset, batch_size=100, shuffle=False)

    embeddings = torch.empty(len(dataset), model.num_features, device=device)
    with torch.no_grad():
        # for i, ((x, _, _), _) in enumerate(loader):
        loop = enumerate(loader)
        for i in range(len(loader)):
            if cfg['dataset'] == 'modelnet10':
                try:
                    _, ((x, _, _), _) = next(loop)
                except Exception as e:
                    print(f"An error occurred: {e}. Using default value instead.")
                    _, (x, _) = next(loop)
            else:
                _, (x, _) = next(loop)

            x = x.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16):
                z = model(x)
            embeddings[i * 100:(i + 1) * 100] = z

    metrics = {}
    if cfg['track_feature_corrs']:
        metrics['corr'] = feature_correlation(embeddings).item()
    if cfg['track_feature_stds']:
        metrics['std'] = feature_std(embeddings).item()
    if cfg['track_feature_entropy']:
        metrics['entropy'] = feature_entropy(embeddings).item()

    return metrics

def eval_representations(
    model: nn.Module,
    cfg: dict
):
    if cfg['dataset'] == 'mnist':
        test = MNIST(cfg['root'], 'test', transform=transforms.ToTensor(), device=cfg['device'], use_tqdm=cfg['local'])
    elif cfg['dataset'] == 'modelnet10':
        test = ModelNet10(cfg['root'], 'test', device=cfg['device'], use_tqdm=cfg['local'], resolution=cfg['resolution'], dataset_dtype=cfg['dataset_dtype'])

    # dataset type is mnist for both as get() returns (x,y)
    metrics = get_rep_metrics(model, test, cfg)
    
    return metrics