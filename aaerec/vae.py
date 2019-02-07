from __future__ import print_function
import argparse
import torch
import torch.utils.data
import torch.nn as nn
import torch.optim as optim

from aaerec.base import Recommender
from aaerec.datasets import Bags
from aaerec.evaluation import Evaluation
from torch.autograd import Variable
import transforms

import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from aaerec.ub import GensimEmbeddedVectorizer
from gensim.models.keyedvectors import KeyedVectors

import scipy.sparse as sp

cuda = torch.cuda.is_available()
torch.manual_seed(42)

W2V_PATH = "/data21/lgalke/vectors/GoogleNews-vectors-negative300.bin.gz"
W2V_IS_BINARY = True

# kwargs = {'num_workers': 1, 'pin_memory': True} if args.cuda else {}
# train_loader = torch.utils.data.DataLoader(
#     datasets.MNIST('../data', train=True, download=True,
#                    transform=transforms.ToTensor()),
#     batch_size=args.batch_size, shuffle=True, **kwargs)
# test_loader = torch.utils.data.DataLoader(
#     datasets.MNIST('../data', train=False, transform=transforms.ToTensor()),
#     batch_size=args.batch_size, shuffle=True, **kwargs)


# bags = Bags.load_tabcomma_format("Data/PMC/citations_pmc.tsv",
#                                  min_count=50,
#                                  min_elements=2)

# old version
# X, Xtest, Ytest = bags.missing_citation_dataset(corrupt_train=False,
#                                                 single_label=True)
#
# Xtest = Xtest.tolil()
# Xtest[Ytest.nonzero()] = 1.0
# Xtest = Xtest.tocsr()
# train_loader = torch.utils.data.DataLoader(X, transforms=[transforms.ToTensor])
# test_loader = torch.utils.data.DataLoader(Xtest,
#                                           transforms=[transforms.ToTensor])


class VAE(nn.Module):

    def __init__(self,
                 inp,
                 n_hidden=100,
                 n_code=50,
                 # TODO In AAE we used both gen_lr and reg_lr. Does regularization still make sense?
                 lr=0.001,
                 batch_size=100,
                 n_epochs=500,
                 # optimizer='adam',
                 normalize_inputs=True,
                 # activation='ReLU',
                 # TODO dropout makes sense?
                 # dropout=(.2,.2),
                 verbose=True,
                 log_interval=1):

        super(VAE, self).__init__()

        self.inp = inp
        self.n_hidden = n_hidden
        self.n_code = n_code
        self.n_epochs = n_epochs
        # TODO parametrize the optimizer
        # self.optimizer = optimizer.lower()
        # TODO in classical AE was helping so it may worth to try it
        # In AE done in forward but VAE compute mean and std in forward to then sample the distrib
        # Here for sure not in the output but not clear where it could be used
        #self.normalize_inputs = normalize_inputs
        self.verbose = verbose
        # TODO see if needed
        # self.dropout = dropout
        self.batch_size = batch_size
        self.lr = lr
        # TODO parametrize activation
        # self.activation = activation

        self.fc1 = nn.Linear(inp, n_hidden)
        self.fc21 = nn.Linear(n_hidden, n_code)
        self.fc22 = nn.Linear(n_hidden, n_code)
        self.fc3 = nn.Linear(n_code, n_hidden)
        self.fc4 = nn.Linear(n_hidden, inp)
        # TODO originally model.parameters(), with model=VAE(bags.size(1)). OK?
        self.optimizer = optim.Adam(self.parameters(), lr=lr)

        # TODO parametrize as self.activation
        self.relu = nn.ReLU()
        self.sigmoid = nn.Sigmoid()

        self.log_interval = log_interval

    def encode(self, x):
        h1 = self.relu(self.fc1(x))
        return self.fc21(h1), self.fc22(h1)

    def reparametrize(self, mu, logvar):
        std = logvar.mul(0.5).exp_()
        if cuda:
            eps = torch.cuda.FloatTensor(std.size()).normal_()
        else:
            eps = torch.FloatTensor(std.size()).normal_()
        eps = Variable(eps)
        return eps.mul(std).add_(mu)

    def decode(self, z):
        h3 = self.relu(self.fc3(z))
        return self.sigmoid(self.fc4(h3))

    def forward(self, x):
        # FIXME What is this magic number 784? Does it generates the error below?
        # RuntimeError: invalid argument 2: size '[-1 x 784]' is invalid
        # for input with 3185 elements at /pytorch/aten/src/TH/THStorage.c:37
        mu, logvar = self.encode(x.view(-1, self.inp))
        z = self.reparametrize(mu, logvar)
        return self.decode(z), mu, logvar

    def loss_function(self, recon_x, x, mu, logvar):
        reconstruction_function = nn.BCELoss()
        reconstruction_function.size_average = False

        BCE = reconstruction_function(recon_x, x)

        # see Appendix B from VAE paper:
        # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
        # https://arxiv.org/abs/1312.6114
        # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
        KLD_element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
        KLD = torch.sum(KLD_element).mul_(-0.5)

        return BCE + KLD

    # TODO may still need some adaptation. E.g. how to use condition?
    def partial_fit(self, X, y=None, condition=None):
        """ Performs reconstruction, discrimination, generator training steps """
        if y is not None:
            raise ValueError("(Semi-)supervised usage not supported")
        # Transform to Torch (Cuda) Variable, shift batch to GPU
        X = Variable(torch.FloatTensor(X))
        if torch.cuda.is_available():
            X = X.cuda()

        if condition is not None:
            condition = condition.astype('float32')
            if sp.issparse(condition):
                condition = condition.toarray()
            condition = Variable(torch.from_numpy(condition))
            if torch.cuda.is_available():
                condition = condition.cuda()

        # Make sure we are in training mode and zero leftover gradients
        self.train()
        train_loss = 0
        # TODO do I need train_loader or obsolete using Evaluation? If needed can be pushed outside (e.g. in class)?
        # FIXME DataLoader transforms?
        # train_loader = torch.utils.data.DataLoader(X, transform=[transforms.ToTensor])
        # for batch_idx, (data, _) in enumerate(train_loader):
        train_loader = torch.utils.data.DataLoader(X)
        for batch_idx, (data) in enumerate(train_loader):
            data = Variable(data)
            if cuda:
                data = data.cuda()
            self.optimizer.zero_grad()
            # TODO originally recon_batch, mu, logvar = model(data), with model = VAE(bags.size(1)). OK?
            recon_batch, mu, logvar = self(data)
            loss = self.loss_function(recon_batch, data, mu, logvar)
            loss.backward()
            train_loss += loss.data[0]
            self.optimizer.step()
            if batch_idx % self.log_interval == 0:
                print('[{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                    batch_idx * len(data), len(train_loader.dataset),
                           100. * batch_idx / len(train_loader),
                           loss.data[0] / len(data)))

        print('====> Average loss: {:.4f}'.format(
            train_loss / len(train_loader.dataset)))
        return self

    # TODO may still need some adaptation. E.g. how to use condition?
    def fit(self, X, y=None, condition=None):
        if y is not None:
            raise NotImplementedError("(Semi-)supervised usage not supported")

        # do the actual training
        for epoch in range(self.n_epochs):
            if self.verbose:
                print("Epoch", epoch + 1)

            # TODO shuffle needed?
            # Shuffle on each new epoch
            if condition is not None:
                X_shuf, condition_shuf = sklearn.utils.shuffle(X, condition)
            else:
                X_shuf = sklearn.utils.shuffle(X)

            for start in range(0, X.shape[0], self.batch_size):
                X_batch = X_shuf[start:(start + self.batch_size)].toarray()
                # condition may be None
                if condition is not None:
                    c_batch = condition_shuf[start:(start + self.batch_size)]
                    self.partial_fit(X_batch, condition=c_batch)
                else:
                    self.partial_fit(X_batch)

            if self.verbose:
                # Clean up after flushing batch loss printings
                print()
        return self

    # TODO handle condition (if it makes sense for VAE)
    def predict(self, X, condition=None):
        self.eval()
        test_loss = 0
        # TODO do I need train_loader or obsolete using Evaluation? If needed can be pushed outside (e.g. in class)?
        # FIXME DataLoader transforms?
        # test_loader = torch.utils.data.DataLoader(X ,
        #                                           transforms=[transforms.ToTensor])
        test_loader = torch.utils.data.DataLoader(X)
        for data, _ in test_loader:
            if cuda:
                data = data.cuda()
            data = Variable(data, volatile=True)
            recon_batch, mu, logvar = self(data)
            test_loss += self.loss_function(recon_batch, data, mu, logvar).data[0]

        test_loss /= len(test_loader.dataset)
        print('====> Test set loss: {:.4f}'.format(test_loss))


# if args.cuda:
#     model.cuda()


# adapted this to our train, now in VAE.partial_fit()
# def train(epoch):
#     model.train()
#     train_loss = 0
#     for batch_idx, (data, _) in enumerate(train_loader):
#         data = Variable(data)
#         if args.cuda:
#             data = data.cuda()
#         optimizer.zero_grad()
#         recon_batch, mu, logvar = model(data)
#         loss = loss_function(recon_batch, data, mu, logvar)
#         loss.backward()
#         train_loss += loss.data[0]
#         optimizer.step()
#         if batch_idx % args.log_interval == 0:
#             print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
#                 epoch, batch_idx * len(data), len(train_loader.dataset),
#                 100. * batch_idx / len(train_loader),
#                 loss.data[0] / len(data)))
#
#     print('====> Epoch: {} Average loss: {:.4f}'.format(
#           epoch, train_loss / len(train_loader.dataset)))

# Now in VAE.predict()
# def test(epoch):
#     model.eval()
#     test_loss = 0
#     for data, _ in test_loader:
#         if args.cuda:
#             data = data.cuda()
#         data = Variable(data, volatile=True)
#         recon_batch, mu, logvar = model(data)
#         test_loss += loss_function(recon_batch, data, mu, logvar).data[0]
#
#     test_loss /= len(test_loader.dataset)
#     print('====> Test set loss: {:.4f}'.format(test_loss))

# for epoch in range(1, args.epochs + 1):
#     train(epoch)
#     test(epoch)

class VAERecommender(Recommender):
    """
    Varietional Autoencoder Recommender
    =====================================

    Arguments
    ---------
    n_input: Dimension of input to expect
    n_hidden: Dimension for hidden layers
    n_code: Code Dimension

    Keyword Arguments
    -----------------
    n_epochs: Number of epochs to train
    batch_size: Batch size to use for training
    verbose: Print losses during training
    normalize_inputs: Whether l1-normalization is performed on the input
    """
    def __init__(self, tfidf_params=dict(),
                 **kwargs):
        """ tfidf_params get piped to either TfidfVectorizer or
        EmbeddedVectorizer.  Remaining kwargs get passed to
        AdversarialAutoencoder """
        super().__init__()
        self.verbose = kwargs.get('verbose', True)
        self.use_title = kwargs.pop('use_title', False)
        self.embedding = kwargs.pop('embedding', None)
        self.vect = None
        self.vae_params = kwargs
        self.tfidf_params = tfidf_params
        self.vae = None

    def __str__(self):
        desc = "Variational Autoencoder"
        desc += " using titles: " + ("Yes!" if self.use_title else "No.")
        desc += '\nVAE Params: ' + str(self.vae_params)
        desc += '\nTfidf Params: ' + str(self.tfidf_params)
        return desc

    def train(self, training_set):
        X = training_set.tocsr()
        if self.use_title:
            if self.embedding:
                self.vect = GensimEmbeddedVectorizer(self.embedding,
                                                     **self.tfidf_params)
            else:
                self.vect = TfidfVectorizer(**self.tfidf_params)

            titles = training_set.get_attribute("title")
            titles = self.vect.fit_transform(titles)
            assert titles.shape[0] == X.shape[0], "Dims dont match"
            # X = sp.hstack([X, titles])
        else:
            titles = None

        # TODO Using X.shape[1] as inp correct? Originally VAE(bags.size(1))
        # IN AAE we do Encoder(X.shape[1],...) 
        self.vae = VAE(X.shape[1], **self.vae_params)
        self.vae.fit(X, condition=titles)

    # TODO reimplement if needed. E.g. How to use condition?
    def predict(self, test_set):
        X = test_set.tocsr()
        if self.use_title:
            # Use titles as condition
            titles = test_set.get_attribute("title")
            titles = self.vect.transform(titles)
            pred = self.vae.predict(X, condition=titles)
        else:
            pred = self.vae.predict(X)

        return pred


def main():
    """ Evaluates the VAE Recommender """
    CONFIG = {
        'pub': ('/data21/lgalke/datasets/citations_pmc.tsv', 2011, 50),
        'eco': ('/data21/lgalke/datasets/econbiz62k.tsv', 2012, 1)
    }

    PARSER = argparse.ArgumentParser()
    PARSER.add_argument('data', type=str, choices=['pub','eco'])
    args = PARSER.parse_args()
    DATA = CONFIG[args.data]
    logfile = '/data22/ivagliano/test-vae/' + args.data + '-decoder.log'
    bags = Bags.load_tabcomma_format(DATA[0])
    c_year = DATA[1]

    evaluate = Evaluation(bags,
                          year=c_year,
                          logfile=logfile).setup(min_count=DATA[2],
                                                 min_elements=2)
    # print("Loading pre-trained embedding", W2V_PATH)
    vectors = KeyedVectors.load_word2vec_format(W2V_PATH, binary=W2V_IS_BINARY)

    params = {
        'n_epochs': 100,
        'batch_size': 100,
        # 'optimizer': 'adam',
        # 'normalize_inputs': True,
        # 'prior': 'gauss',
    }
    # 100 hidden units, 200 epochs, bernoulli prior, normalized inputs -> 0.174
    # activations = ['ReLU','SELU']
    # lrs = [(0.001, 0.0005), (0.001, 0.001)]
    #hcs = [(100, 50), (300, 100)]


    # dropouts = [(.2,.2), (.1,.1), (.1, .2), (.25, .25), (.3,.3)] # .2,.2 is best
    # priors = ['categorical'] # gauss is best
    # normal = [True, False]
    # bernoulli was good, letz see if categorical is better... No
    # import itertools
    # models = [VAERecommender(**params, n_hidden=hc[0], n_code=hc[1],
    #                          use_title=ut, embedding=vectors,
    #                          gen_lr=lr[0], reg_lr=lr[1], activation=a)
    #           for ut, lr, hc, a in itertools.product((True, False), lrs, hcs, activations)]
    models = [VAERecommender(**params,use_title=False, embedding=vectors)]
    evaluate(models)


if __name__ == '__main__':
    main()