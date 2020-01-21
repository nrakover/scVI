import os
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import precision_score, recall_score, accuracy_score
from tqdm.auto import tqdm

from scvi.dataset import MnistDataset
from scvi.inference import MnistTrainer
from scvi.models import SemiSupervisedVAE
from arviz.stats import psislw

NUM = 300
N_EXPERIMENTS = 1
labelled_proportions = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.0])
labelled_proportions = labelled_proportions / labelled_proportions.sum()
labelled_fraction = 0.05

n_input = 28 * 28
n_labels = 9

CLASSIFICATION_RATIO = 50.0
N_EVAL_SAMPLES = 25
N_EPOCHS = 100
LR = 3e-4
BATCH_SIZE = 1024

FILENAME = "simu_mnist2.pkl"
MDL_DIR = "models/mnist3"

if not os.path.exists(MDL_DIR):
    os.makedirs(MDL_DIR)

dataset = MnistDataset(
    labelled_fraction=labelled_fraction,
    labelled_proportions=labelled_proportions,
    root="/home/pierre/scVI/tests/mnist",
    download=True,
    do_1d=True,
    test_size=0.0,
    # test_size=0.0,
)
x_train, y_train = dataset.train_dataset.tensors
rdm_indices = np.random.choice(len(x_train), 64)
x_sample = x_train[rdm_indices].to("cuda")
y_sample = y_train[rdm_indices].to("cuda")

where = y_train == 8
x_sample_supervised = x_train[where][:64]

print("train all examples", len(dataset.train_dataset.tensors[0]))
print("train labelled examples", len(dataset.train_dataset_labelled.tensors[0]))

scenarios = [  # WAKE updates
    # ( overall_training, loss_gen, loss_wvar, loss_svar, n_samples_train, n_samples_wtheta, n_samples_wphi,)
    # dict(
    #     loss_gen="ELBO",
    #     loss_wvar="CUBOB",
    #     n_samples_train=None,
    #     n_samples_wtheta=15,
    #     n_samples_wphi=15,
    #     reparam_latent=True,
    #     n_epochs=100,
    #     lr=3e-4,
    # ),

    dict(
        loss_gen="ELBO",
        loss_wvar="CUBOB",
        n_samples_train=None,
        n_samples_wtheta=15,
        n_samples_wphi=15,
        reparam_latent=True,
        z2_with_elbo=True,
        n_epochs=75,
        n_hidden=128,
        n_latent=10,
        lr=1e-3,
    ),

    dict(
        loss_gen="ELBO",
        loss_wvar="CUBOB",
        n_samples_train=None,
        n_samples_wtheta=15,
        n_samples_wphi=15,
        reparam_latent=True,
        z2_with_elbo=False,
        n_epochs=75,
        n_hidden=128,
        n_latent=10,
        lr=1e-3,
    ),

    dict(
        loss_gen="ELBO",
        loss_wvar="REVKL",
        n_samples_train=None,
        n_samples_wtheta=15,
        n_samples_wphi=15,
        reparam_latent=False,
        z2_with_elbo=False,
        n_epochs=75,
        n_hidden=128,
        n_latent=10,
        lr=1e-3,
    ),

    dict(
        loss_gen="ELBO",
        loss_wvar="REVKL",
        n_samples_train=None,
        n_samples_wtheta=15,
        n_samples_wphi=15,
        reparam_latent=False,
        z2_with_elbo=True,
        n_epochs=75,
        n_hidden=128,
        n_latent=10,
        lr=1e-3,
    ),

    dict(
        loss_gen="ELBO",
        loss_wvar="ELBO",
        n_samples_train=1,
        n_samples_wtheta=None,
        n_samples_wphi=None,
        reparam_latent=True,
        z2_with_elbo=False,
        n_epochs=75,
        n_hidden=128,
        n_latent=10,
        lr=1e-3,
    ),

]

df_li = []


# Utils functions
def compute_reject_score(y_true: np.ndarray, y_pred: np.ndarray, num=20):
    n_examples, n_pos_classes = y_pred.shape

    assert np.unique(y_true).max() == (n_pos_classes - 1) + 1
    thetas = np.linspace(0.1, 1.0, num=num)
    res = dict(
        precision_discovery=np.zeros(num),
        recall_discovery=np.zeros(num),
        accuracy=np.zeros(num),
        thresholds=thetas,
    )

    for idx, theta in enumerate(thetas):
        y_pred_theta = y_pred.argmax(1)
        reject = y_pred.max(1) <= theta
        y_pred_theta[reject] = (n_pos_classes - 1) + 1

        res["accuracy"][idx] = accuracy_score(y_true, y_pred_theta)

        y_true_discovery = y_true == (n_pos_classes - 1) + 1
        y_pred_discovery = y_pred_theta == (n_pos_classes - 1) + 1
        res["precision_discovery"][idx] = precision_score(
            y_true_discovery, y_pred_discovery
        )
        res["recall_discovery"][idx] = recall_score(y_true_discovery, y_pred_discovery)
    return res


# Main script
for scenario in scenarios:
    loss_gen = scenario["loss_gen"]
    loss_wvar = scenario["loss_wvar"]
    n_samples_train = scenario["n_samples_train"]
    n_samples_wtheta = scenario["n_samples_wtheta"]
    n_samples_wphi = scenario["n_samples_wphi"]
    reparam_latent = scenario["reparam_latent"]
    n_epochs = scenario["n_epochs"]
    n_latent = scenario["n_latent"]
    n_hidden = scenario["n_hidden"]
    lr = scenario["lr"]
    z2_with_elbo = scenario["z2_with_elbo"]

    iwelbo = []
    cubo = []
    khat = []
    khat1e4 = []
    ess = []
    khat1e4_supervised = []
    ess_supervised = []
    m_accuracy_arr = []
    m_ap_arr = []
    m_recall_arr = []
    auc_pr_arr = []
    entropy_arr = []

    for t in range(N_EXPERIMENTS):
        scenario["num"] = t
        mdl_name = ""
        for st in scenario.values():
            mdl_name = mdl_name + str(st) + "_"
        mdl_name = str(mdl_name)
        mdl_name = os.path.join(MDL_DIR, "{}.pt".format(mdl_name))
        print(mdl_name)
        mdl = SemiSupervisedVAE(
            n_input=n_input,
            n_labels=n_labels,
            n_latent=n_latent,
            n_hidden=n_hidden,
            n_layers=1,
            do_batch_norm=True,
        )
        if os.path.exists(mdl_name):
            print("model exists; loading from .pt")
            mdl.load_state_dict(torch.load(mdl_name))
        mdl.cuda()
        trainer = MnistTrainer(
            dataset=dataset, model=mdl, use_cuda=True, batch_size=BATCH_SIZE
        )

        try:
            if not os.path.exists(mdl_name):
                if (loss_gen == "ELBO") and (loss_wvar == "ELBO"):
                    overall_loss = "ELBO"
                else:
                    overall_loss = None
                trainer.train(
                    n_epochs=n_epochs,
                    lr=lr,
                    overall_loss=overall_loss,
                    wake_theta=loss_gen,
                    wake_psi=loss_wvar,
                    n_samples=n_samples_train,
                    n_samples_theta=n_samples_wtheta,
                    n_samples_phi=n_samples_wphi,
                    reparam_wphi=reparam_latent,
                    classification_ratio=CLASSIFICATION_RATIO,
                    z2_with_elbo=z2_with_elbo,
                    update_mode="all",
                )
            torch.save(mdl.state_dict(), mdl_name)

            # Eval
            with torch.no_grad():
                train_res = trainer.inference(
                    trainer.train_loader,
                    keys=["qc_z1_all_probas", "y", "CUBO", "IWELBO", "log_ratios"],
                    n_samples=N_EVAL_SAMPLES,
                )
            y_pred = train_res["qc_z1_all_probas"].mean(0).numpy()
            # log_ratios = train_res["log_ratios"].permute(2, 0, 1)
            # weights = torch.softmax(log_ratios, dim=1)
            y_true = train_res["y"].numpy()

            # Choice right now: all log-ratios related metrics are computed in the unsupervised case

            # Precision / Recall for discovery class
            # And accuracy
            res_baseline = compute_reject_score(y_true=y_true, y_pred=y_pred, num=NUM)
            # m_accuracy = res_baseline["accuracy"]
            m_ap = res_baseline["precision_discovery"]
            m_recall = res_baseline["recall_discovery"]
            auc_pr = np.trapz(
                x=res_baseline["recall_discovery"],
                y=res_baseline["precision_discovery"],
            )

            # m_accuracy_arr.append(m_accuracy)
            m_ap_arr.append(m_ap)
            m_recall_arr.append(m_recall)
            auc_pr_arr.append(auc_pr)

            # Cubo / Iwelbo
            cubo_sam = train_res["CUBO"]
            cubo.append(cubo_sam.mean())

            iwelbo_sam = train_res["IWELBO"]
            iwelbo.append(iwelbo_sam.mean())

            # Entropy
            where9 = train_res["y"] == 9
            probas9 = train_res["qc_z1_all_probas"].mean(0)[where9]
            entropy_arr.append((-probas9 * probas9.log()).sum(-1).mean(0))

            where_non9 = train_res["y"] != 9
            y_non9 = train_res["y"][where_non9]
            y_pred_non9 = train_res["qc_z1_all_probas"].mean(0)[where_non9].argmax(1)
            m_accuracy = accuracy_score(y_non9, y_pred_non9)
            m_accuracy_arr.append(m_accuracy)

            # k_hat
            ## Old version taking few samples
            log_ratios = train_res["log_ratios"].cpu().numpy()
            n_examples = log_ratios.shape[-1]
            log_ratios = log_ratios.reshape((-1, n_examples)).T
            assert log_ratios.shape[0] == n_examples
            _, khat_vals = psislw(log_ratios)
            khat.append(khat_vals)

            ## New version
            log_ratios = []
            qc_z = []
            n_samples_total = 1e4
            n_samples_per_pass = 25
            n_iter = int(n_samples_total / n_samples_per_pass)

            ### Unsupervised case
            for _ in tqdm(range(n_iter)):
                with torch.no_grad():
                    out = mdl.inference(x_sample, n_samples=n_samples_per_pass)
                log_ratio = (
                    out["log_px_z"]
                    + out["log_pz2"]
                    + out["log_pc"]
                    + out["log_pz1_z2"]
                    - out["log_qz1_x"]
                    - out["log_qc_z1"]
                    - out["log_qz2_z1"]
                ).cpu()
                qc_z_here = out["log_qc_z1"].cpu().exp()

                qc_z.append(qc_z_here)
                log_ratios.append(log_ratio)
            # Concatenation over samples
            log_ratios = torch.cat(log_ratios, 1)
            qc_z = torch.cat(qc_z, 1)
            log_ratios = (log_ratios * qc_z).sum(0)  # Sum over labels

            _, khats = psislw(log_ratios.T.clone())
            wi = torch.softmax(log_ratios, 0)
            ess_here = 1.0 / (wi**2).sum(0)

            khat1e4.append(khats)
            ess.append(ess_here.numpy())

            ### Supervised case
            log_ratios = []
            for _ in tqdm(range(n_iter)):
                with torch.no_grad():
                    out = mdl.inference(x_sample, n_samples=n_samples_per_pass)
                out = (
                    out["log_px_z"]
                    + out["log_pz2"]
                    + out["log_pz1_z2"]
                    - out["log_qz1_x"]
                    - out["log_qz2_z1"]
                )
                log_ratios.append(out.cpu())
            log_ratios = torch.cat(log_ratios, dim=1)
            log_ratios = log_ratios[8].T  # Only take label of interest

            _, khats = psislw(log_ratios.clone())
            wi = torch.softmax(log_ratios, 0)
            ess_here = 1.0 / (wi**2).sum(0)

            khat1e4_supervised.append(khats)
            ess_supervised.append(ess_here.numpy())
        except Exception as e:
            print(e)
            pass

    res = {
        "CONFIGURATION": scenario,
        "LOSS_GEN": loss_gen,
        "LOSS_WVAR": loss_wvar,
        "N_SAMPLES_TRAIN": n_samples_train,
        "N_SAMPLES_WTHETA": n_samples_wtheta,
        "N_SAMPLES_WPHI": n_samples_wphi,
        "REPARAM_LATENT": reparam_latent,
        "N_LATENT": n_latent,
        "N_HIDDEN": n_hidden,
        "N_EPOCHS": n_epochs,
        "LR": lr,
        "Z2_WITH_ELBO": z2_with_elbo,
        "IWELBO": (np.mean(iwelbo), np.std(iwelbo)),
        "IWELBO_SAMPLES": np.array(iwelbo),
        "CUBO": (np.mean(cubo), np.std(cubo)),
        "CUBO_SAMPLES": np.array(cubo),
        "KHAT": np.array(khat),
        "KHAT1e4": np.array(khat1e4),
        "ESS": np.array(ess),
        "KHAT1e4_supervised": np.array(khat1e4_supervised),
        "ESS_supervised": np.array(ess_supervised),
        "M_ACCURACY": np.array(m_accuracy_arr),
        "MEAN_AP": np.array(m_ap_arr),
        "MEAN_RECALL": np.array(m_recall_arr),
        "AUC": np.array(auc_pr_arr),
        "ENTROPY": np.array(entropy_arr),
    }
    print(res)
    df_li.append(res)
    df = pd.DataFrame(df_li)
    df.to_pickle(FILENAME)

df = pd.DataFrame(df_li)
df.to_pickle(FILENAME)
